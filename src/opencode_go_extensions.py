# -*- coding: utf-8 -*-
"""Fork-local OpenCode Go extensions used by ``python main.py``.

The extension keeps two production customizations isolated from upstream code:

* Exa hosted MCP is the first news-search provider; the repository's existing
  providers (including SearXNG) remain fail-open fallbacks.
* A-share brief reports always cover both leading and lagging sectors.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests


logger = logging.getLogger(__name__)
_EXA_MCP_URL = "https://mcp.exa.ai/mcp?tools=web_search_advanced_exa"
_EXA_PROVIDER_NAME = "ExaMCP"


def _is_enabled(name: str, *, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _is_brief_mode() -> bool:
    value = (
        os.getenv("MARKET_REVIEW_REPORT_TYPE")
        or os.getenv("REPORT_TYPE")
        or ""
    ).strip().lower()
    return value in {"simple", "brief"}


def _json_object(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _parse_mcp_envelope(body: str) -> Optional[Dict[str, Any]]:
    """Return the result-bearing JSON-RPC message from JSON or SSE output."""
    text = str(body or "").strip()
    if not text:
        return None

    direct = _json_object(text)
    if direct is not None:
        return direct

    candidates: List[Dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        parsed = _json_object(payload)
        if parsed is not None:
            candidates.append(parsed)

    # Streamable HTTP may emit progress/metadata events before the actual tool
    # result.  Prefer the last result/error message instead of the first event.
    for item in reversed(candidates):
        if "result" in item or "error" in item:
            return item
    return candidates[-1] if candidates else None


def _find_results(value: Any) -> Optional[List[Dict[str, Any]]]:
    """Find Exa's structured ``results`` array in an MCP response."""
    if isinstance(value, dict):
        rows = value.get("results")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]

        # Advanced Exa MCP commonly serializes its structured payload into a
        # text content item.  Decode JSON-looking text before falling back.
        content = value.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                nested = _find_results(item)
                if nested is not None:
                    return nested
                text = item.get("text")
                parsed = _json_object(text)
                if parsed is not None:
                    nested = _find_results(parsed)
                    if nested is not None:
                        return nested

        for key in ("structuredContent", "data", "result"):
            if key in value:
                nested = _find_results(value[key])
                if nested is not None:
                    return nested

    if isinstance(value, list):
        for item in value:
            nested = _find_results(item)
            if nested is not None:
                return nested
    return None


def _collect_text(value: Any) -> str:
    blocks: List[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            content = node.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text")
                        if isinstance(text, str) and text.strip():
                            blocks.append(text.strip())
            for key in ("structuredContent", "data", "result"):
                if key in node:
                    visit(node[key])
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return "\n\n".join(blocks)


def _parse_text_results(text: str) -> List[Dict[str, Any]]:
    """Compatibility parser for Exa's Title/URL/Published Date text format."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    matches = list(re.finditer(r"(?m)^Title:\s*([^\n]+)", normalized))
    rows: List[Dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        section = normalized[match.start():end]

        def field(label: str) -> Optional[str]:
            found = re.search(rf"(?m)^{re.escape(label)}:\s*([^\n]*)", section)
            value = found.group(1).strip() if found else ""
            return value or None

        body_match = re.search(r"(?ms)^Text:\s*(.*)$", section)
        rows.append(
            {
                "title": match.group(1).strip(),
                "url": field("URL"),
                "publishedDate": field("Published Date"),
                "text": body_match.group(1).strip() if body_match else "",
            }
        )
    return rows


def _extract_exa_results(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    error = envelope.get("error")
    if isinstance(error, dict):
        raise RuntimeError(str(error.get("message") or error))
    if error:
        raise RuntimeError(str(error))

    payload = envelope.get("result", envelope)
    rows = _find_results(payload)
    if rows is not None:
        return rows
    return _parse_text_results(_collect_text(payload))


def _first_value(mapping: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    metadata = mapping.get("metadata")
    if isinstance(metadata, dict):
        for key in keys:
            value = metadata.get(key)
            if value not in (None, ""):
                return value
    return None


def _published_date(item: Dict[str, Any]) -> Optional[str]:
    raw = _first_value(
        item,
        "publishedDate",
        "published_date",
        "publishedAt",
        "published_at",
        "date",
    )
    text = str(raw or "").strip()
    if not text:
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return match.group(0) if match else text


def _source(url: str) -> str:
    try:
        return urlparse(url).netloc.removeprefix("www.") or "Exa"
    except Exception:
        return "Exa"


def _install_exa_search_provider() -> None:
    if not _is_enabled("EXA_MCP_SEARCH_ENABLED", default=True):
        return

    from src.search_service import BaseSearchProvider, SearchResponse, SearchResult, SearchService

    if getattr(SearchService, "_opencode_exa_mcp_installed", False):
        return

    class ExaMcpSearchProvider(BaseSearchProvider):
        """Hosted Exa advanced news search; no key required, key optional."""

        def __init__(self) -> None:
            super().__init__([], _EXA_PROVIDER_NAME)

        @property
        def is_available(self) -> bool:
            return True

        def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
            return self.search(query, max_results=max_results, days=days)

        def search(
            self,
            query: str,
            max_results: int = 5,
            days: int = 7,
            **_kwargs: Any,
        ) -> SearchResponse:
            started = time.time()
            requested = max(1, min(int(max_results or 5), 10))
            search_query = str(query or "").strip()
            if not search_query:
                return SearchResponse(
                    query=query,
                    results=[],
                    provider=self.name,
                    success=False,
                    error_message="搜索关键词为空",
                )

            # Match the repository's freshness window at the search provider,
            # rather than accepting undated results and pretending they are new.
            window_days = max(1, int(days or 1))
            today = date.today()
            start_date = today - timedelta(days=window_days - 1)
            end_date = today + timedelta(days=1)
            arguments: Dict[str, Any] = {
                "query": search_query,
                "category": "news",
                "numResults": requested,
                "type": "auto",
                "startPublishedDate": start_date.isoformat(),
                "endPublishedDate": end_date.isoformat(),
                "textMaxCharacters": 900,
            }
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "web_search_advanced_exa",
                    "arguments": arguments,
                },
            }
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": "daily-stock-analysis/ExaMCP",
            }
            exa_key = (os.getenv("EXA_API_KEY") or "").strip()
            if exa_key:
                headers["x-api-key"] = exa_key

            try:
                response = requests.post(
                    _EXA_MCP_URL,
                    headers=headers,
                    json=body,
                    timeout=20,
                )
                elapsed = time.time() - started
                if response.status_code != 200:
                    detail = (response.text or "").strip()[:300]
                    return SearchResponse(
                        query=query,
                        results=[],
                        provider=self.name,
                        success=False,
                        error_message=f"HTTP {response.status_code}: {detail}",
                        search_time=elapsed,
                    )

                envelope = _parse_mcp_envelope(response.text)
                if envelope is None:
                    return SearchResponse(
                        query=query,
                        results=[],
                        provider=self.name,
                        success=False,
                        error_message="Exa MCP 响应无法解析",
                        search_time=elapsed,
                    )

                raw_rows = _extract_exa_results(envelope)
                results: List[SearchResult] = []
                for item in raw_rows:
                    url = str(item.get("url") or "").strip()
                    if not url:
                        continue
                    title = str(item.get("title") or url).strip()
                    highlights = item.get("highlights")
                    snippet = str(item.get("summary") or item.get("text") or "").strip()
                    if not snippet and isinstance(highlights, list):
                        snippet = " ".join(str(part) for part in highlights if part).strip()
                    snippet = re.sub(r"\s+", " ", snippet)[:700]
                    results.append(
                        SearchResult(
                            title=title[:180],
                            snippet=snippet,
                            url=url,
                            source=_source(url),
                            published_date=_published_date(item),
                        )
                    )
                    if len(results) >= requested:
                        break

                dated = sum(1 for item in results if item.published_date)
                logger.info(
                    "[%s] 搜索 '%s' 完成，窗口=%s..%s，返回 %s 条（有日期 %s 条），耗时 %.2fs%s",
                    self.name,
                    search_query,
                    start_date,
                    end_date,
                    len(results),
                    dated,
                    elapsed,
                    "（API Key）" if exa_key else "（公共 MCP）",
                )
                return SearchResponse(
                    query=query,
                    results=results,
                    provider=self.name,
                    success=True,
                    search_time=elapsed,
                )
            except Exception as exc:
                elapsed = time.time() - started
                logger.warning("[%s] 搜索失败: %s", self.name, exc)
                return SearchResponse(
                    query=query,
                    results=[],
                    provider=self.name,
                    success=False,
                    error_message=str(exc),
                    search_time=elapsed,
                )

    original_init = SearchService.__init__

    def patched_init(self, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        if not any(getattr(p, "name", "") == _EXA_PROVIDER_NAME for p in self._providers):
            self._providers.insert(0, ExaMcpSearchProvider())
            logger.info("已启用 Exa MCP 实时新闻搜索，SearXNG/其他引擎作为后续兜底")

    SearchService.__init__ = patched_init
    SearchService._opencode_exa_mcp_installed = True


def _signed_pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "涨跌幅未知"


def _ranking_summary(rows: Any, *, limit: int = 2) -> str:
    if not isinstance(rows, list):
        return ""
    items: List[str] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if name:
            items.append(f"{name} {_signed_pct(row.get('change_pct'))}")
    return "；".join(items)


def _insert_before_tail(text: str, block: str) -> str:
    if not block:
        return text
    for marker in ("\n**消息**", "\n**关注**", "\n**风险**", "\n**结论**"):
        pos = text.find(marker)
        if pos >= 0:
            return text[:pos].rstrip() + "\n" + block + "\n" + text[pos:].lstrip("\n")
    return text.rstrip() + "\n" + block


def _ensure_cn_sector_balance(text: str, overview: Any) -> str:
    """Deterministically keep both strongest and weakest A-share sectors."""
    report = str(text or "").strip()
    if not report:
        return report

    top_rows = getattr(overview, "top_sectors", None) or getattr(overview, "top_concepts", None) or []
    bottom_rows = getattr(overview, "bottom_sectors", None) or getattr(overview, "bottom_concepts", None) or []

    if "**领涨**" not in report and "**主线**" in report:
        report = report.replace("**主线**", "**领涨**", 1)

    if "**领涨**" not in report:
        summary = _ranking_summary(top_rows)
        if summary:
            report = _insert_before_tail(report, f"**领涨** {summary}")

    if "**领跌**" not in report:
        summary = _ranking_summary(bottom_rows)
        if summary:
            report = _insert_before_tail(report, f"**领跌** {summary}")

    return report


def _install_balanced_sector_brief_patch() -> None:
    from src.market_analyzer import MarketAnalyzer

    if getattr(MarketAnalyzer, "_opencode_sector_balance_installed", False):
        return

    original_prompt = MarketAnalyzer._build_review_prompt

    def balanced_prompt(self, overview, news):
        prompt = original_prompt(self, overview, news)
        if not _is_brief_mode() or getattr(self, "region", "cn") != "cn":
            return prompt
        return prompt + """

【A股板块双向覆盖规则｜优先级高于上面的简报格式】
最终 A 股简报不能只写上涨主线，必须同时记录当日最强与最弱板块：
- 将原来的“主线”拆为“领涨”和“领跌”两个独立信息块。
- 领涨：从输入的行业/概念领涨榜选 1-2 个最强方向，写“板块 + 涨幅 + 一句话判断”。
- 领跌：从输入的行业/概念领跌榜选 1-2 个最弱方向，写“板块 + 跌幅 + 一句话判断”。
- 涨跌幅必须来自输入数据；没有可靠新闻解释原因时，只做盘面判断，不得编造政策、资金或消息原因。
- 关注与结论同时考虑强势方向的持续性和弱势方向是否扩散。
- 保持 900 字以内，禁止 Markdown 表格。
- 顺序固定：市场、领涨、领跌、消息、关注、风险、结论。

推荐格式：
## A股简报
> 一句话市场状态
**市场** 指数/情绪/成交核心数据
**领涨**
- 最强板块1：涨幅 + 判断
- 最强板块2：涨幅 + 判断
**领跌**
- 最弱板块1：跌幅 + 判断
- 最弱板块2：跌幅 + 判断
**消息** 最多2条可靠增量新闻
**关注** 强势延续观察点；弱势扩散观察点
**风险** 一句话
**结论** 一句话概括市场强弱、最强/最弱方向和操作节奏
"""

    MarketAnalyzer._build_review_prompt = balanced_prompt

    original_inject = MarketAnalyzer._inject_data_into_review

    def balanced_inject(self, review, overview, news=None):
        rendered = original_inject(self, review, overview, news)
        if _is_brief_mode() and getattr(self, "region", "cn") == "cn":
            return _ensure_cn_sector_balance(rendered, overview)
        return rendered

    MarketAnalyzer._inject_data_into_review = balanced_inject
    MarketAnalyzer._opencode_sector_balance_installed = True


def install() -> None:
    _install_exa_search_provider()
    _install_balanced_sector_brief_patch()
