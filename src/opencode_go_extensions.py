# -*- coding: utf-8 -*-
"""OpenCode Go runtime extensions for the daily-analysis entrypoint.

This module is loaded only by ``src.__init__`` when ``python main.py`` is the
entrypoint.  It keeps fork-specific integration changes isolated from upstream
search/report code:

1. Add the hosted Exa MCP ``web_search_exa`` tool as the first keyless search
   provider.  Existing providers, including SearXNG, remain fallback options.
2. Make A-share brief reports explicitly cover both leading and lagging sectors
   so Enterprise WeChat summaries do not collapse sector analysis into only the
   strongest theme.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests


logger = logging.getLogger(__name__)
_EXA_MCP_URL = "https://mcp.exa.ai/mcp?tools=web_search_exa"
_EXA_PROVIDER_NAME = "ExaMCP"


def _is_enabled(name: str, *, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _is_brief_mode() -> bool:
    report_type = (
        os.getenv("MARKET_REVIEW_REPORT_TYPE")
        or os.getenv("REPORT_TYPE")
        or ""
    ).strip().lower()
    return report_type in {"simple", "brief"}


def _parse_json_object(raw: str) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _parse_mcp_envelope(body: str) -> Optional[Dict[str, Any]]:
    """Parse a Streamable-HTTP MCP response in JSON or SSE form."""
    text = str(body or "").strip()
    if not text:
        return None

    direct = _parse_json_object(text)
    if direct is not None:
        return direct

    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        parsed = _parse_json_object(payload)
        if parsed is not None:
            return parsed
    return None


def _find_structured_results(value: Any) -> Optional[List[Dict[str, Any]]]:
    """Find an Exa ``results`` array in nested MCP/structured content."""
    if isinstance(value, dict):
        raw_results = value.get("results")
        if isinstance(raw_results, list):
            return [item for item in raw_results if isinstance(item, dict)]

        for key in ("structuredContent", "data", "result"):
            nested = _find_structured_results(value.get(key))
            if nested is not None:
                return nested

        content = value.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                nested = _find_structured_results(item)
                if nested is not None:
                    return nested
                raw_text = item.get("text")
                if isinstance(raw_text, str):
                    parsed = _parse_json_object(raw_text.strip())
                    if parsed is not None:
                        nested = _find_structured_results(parsed)
                        if nested is not None:
                            return nested

    if isinstance(value, list):
        for item in value:
            nested = _find_structured_results(item)
            if nested is not None:
                return nested
    return None


def _collect_mcp_text(value: Any) -> str:
    blocks: List[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            content = node.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        raw_text = item.get("text")
                        if isinstance(raw_text, str) and raw_text.strip():
                            blocks.append(raw_text.strip())
            for key in ("structuredContent", "data", "result"):
                if key in node:
                    visit(node[key])
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return "\n\n".join(blocks)


def _parse_optional_field(section: str, label: str) -> Optional[str]:
    match = re.search(rf"(?:^|\n){re.escape(label)}:\s*([^\n]*)", section)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _parse_exa_text_results(text: str) -> List[Dict[str, Any]]:
    """Parse the text form returned by hosted Exa MCP.

    Exa commonly returns blocks shaped as ``Title / URL / Author /
    Published Date / Text``.  Parsing by title boundaries also tolerates an
    omitted author/date and the newer ``Highlights`` fallback.
    """
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    title_matches = list(re.finditer(r"(?m)^Title:\s*([^\n]*)", normalized))
    results: List[Dict[str, Any]] = []
    for index, match in enumerate(title_matches):
        end = title_matches[index + 1].start() if index + 1 < len(title_matches) else len(normalized)
        section = normalized[match.start():end].strip().strip("-").strip()
        title = match.group(1).strip()
        url = _parse_optional_field(section, "URL")
        author = _parse_optional_field(section, "Author")
        published_date = _parse_optional_field(section, "Published Date")

        text_match = re.search(r"(?:^|\n)Text:\s*([\s\S]*)$", section)
        snippet = text_match.group(1).strip() if text_match else ""
        if not snippet:
            highlights_match = re.search(r"(?:^|\n)Highlights?:\s*([\s\S]*)$", section)
            snippet = highlights_match.group(1).strip() if highlights_match else ""
        snippet = re.sub(r"\n-{3,}\s*$", "", snippet).strip()

        if title or url or snippet:
            results.append(
                {
                    "title": title,
                    "url": url,
                    "author": author,
                    "publishedDate": published_date,
                    "text": snippet,
                }
            )
    return results


def _extract_exa_results(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    error = envelope.get("error")
    if isinstance(error, dict):
        raise RuntimeError(str(error.get("message") or error))
    if error:
        raise RuntimeError(str(error))

    result = envelope.get("result", envelope)
    structured = _find_structured_results(result)
    if structured is not None:
        return structured

    return _parse_exa_text_results(_collect_mcp_text(result))


def _normalize_published_date(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}", raw)
    return match.group(0) if match else raw


def _source_from_url(url: str) -> str:
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
        """Keyless hosted Exa MCP provider with optional EXA_API_KEY upgrade."""

        def __init__(self) -> None:
            super().__init__([], _EXA_PROVIDER_NAME)

        @property
        def is_available(self) -> bool:
            return True

        def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
            # BaseSearchProvider.search is overridden below, so this method is
            # present only to satisfy the abstract interface.
            return self.search(query, max_results=max_results, days=days)

        def search(
            self,
            query: str,
            max_results: int = 5,
            days: int = 7,
            **_kwargs: Any,
        ) -> SearchResponse:
            start = time.time()
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

            # The simple Exa MCP tool supports an inline news category.  Add it
            # for short freshness windows while keeping long-horizon analytical
            # searches general-purpose.
            exa_query = search_query
            if days <= 30 and not search_query.lower().startswith("category:"):
                exa_query = f"category:news {search_query}"

            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "web_search_exa",
                    "arguments": {
                        "query": exa_query,
                        "type": "auto",
                        "numResults": requested,
                        "livecrawl": "fallback",
                        "contextMaxCharacters": max(3000, requested * 900),
                    },
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
                elapsed = time.time() - start
                if response.status_code != 200:
                    error_text = (response.text or "").strip()[:300]
                    return SearchResponse(
                        query=query,
                        results=[],
                        provider=self.name,
                        success=False,
                        error_message=f"HTTP {response.status_code}: {error_text}",
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

                raw_results = _extract_exa_results(envelope)
                results: List[SearchResult] = []
                for item in raw_results:
                    url = str(item.get("url") or "").strip()
                    if not url:
                        continue
                    title = str(item.get("title") or url).strip()
                    summary = item.get("summary")
                    text = item.get("text")
                    highlights = item.get("highlights")
                    snippet = str(summary or text or "").strip()
                    if not snippet and isinstance(highlights, list):
                        snippet = " ".join(str(part) for part in highlights if part).strip()
                    snippet = re.sub(r"\s+", " ", snippet)[:700]
                    results.append(
                        SearchResult(
                            title=title[:180],
                            snippet=snippet,
                            url=url,
                            source=_source_from_url(url),
                            published_date=_normalize_published_date(
                                item.get("publishedDate") or item.get("published_date")
                            ),
                        )
                    )
                    if len(results) >= requested:
                        break

                logger.info(
                    "[%s] 搜索 '%s' 完成，返回 %s 条结果，耗时 %.2fs%s",
                    self.name,
                    search_query,
                    len(results),
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
                elapsed = time.time() - start
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
        if not any(getattr(provider, "name", "") == _EXA_PROVIDER_NAME for provider in self._providers):
            self._providers.insert(0, ExaMcpSearchProvider())
            logger.info("已启用 Exa MCP 公共新闻搜索，SearXNG/其他引擎作为后续兜底")

    SearchService.__init__ = patched_init
    SearchService._opencode_exa_mcp_installed = True


def _format_signed_pct(value: Any) -> str:
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
        if not name:
            continue
        items.append(f"{name} {_format_signed_pct(row.get('change_pct'))}")
    return "；".join(items)


def _insert_before_decision_tail(text: str, block: str) -> str:
    if not block:
        return text
    for marker in ("\n**消息**", "\n**关注**", "\n**风险**", "\n**结论**"):
        pos = text.find(marker)
        if pos >= 0:
            return text[:pos].rstrip() + "\n" + block + "\n" + text[pos:].lstrip("\n")
    return text.rstrip() + "\n" + block


def _ensure_cn_sector_balance(text: str, overview: Any) -> str:
    """Guarantee concise leading + lagging sector coverage from structured data."""
    report = str(text or "").strip()
    if not report:
        return report

    top_rows = getattr(overview, "top_sectors", None) or getattr(overview, "top_concepts", None) or []
    bottom_rows = getattr(overview, "bottom_sectors", None) or getattr(overview, "bottom_concepts", None) or []

    # The previous brief format called strongest themes ``主线``.  In A-share
    # mode this is semantically the leading side, so relabel it once rather than
    # keeping a duplicate Mainline + Leading block.
    if "**领涨**" not in report and "**主线**" in report:
        report = report.replace("**主线**", "**领涨**", 1)

    if "**领涨**" not in report:
        top_summary = _ranking_summary(top_rows)
        if top_summary:
            report = _insert_before_decision_tail(report, f"**领涨** {top_summary}")

    if "**领跌**" not in report:
        bottom_summary = _ranking_summary(bottom_rows)
        if bottom_summary:
            report = _insert_before_decision_tail(report, f"**领跌** {bottom_summary}")

    return report


def _install_balanced_sector_brief_patch() -> None:
    from src.market_analyzer import MarketAnalyzer

    if getattr(MarketAnalyzer, "_opencode_sector_balance_installed", False):
        return

    original_prompt_builder = MarketAnalyzer._build_review_prompt

    def balanced_prompt_builder(self, overview, news):
        prompt = original_prompt_builder(self, overview, news)
        if not _is_brief_mode() or getattr(self, "region", "cn") != "cn":
            return prompt

        return prompt + """

【A股板块双向覆盖规则｜优先级高于上面的简报格式】
最终 A 股简报不能只写上涨主线，必须同时记录当日最强与最弱板块：
- 将原来的“主线”拆成“领涨”和“领跌”两个独立信息块。
- 领涨：从已提供的行业/概念领涨榜中选 1-2 个最强方向，写“板块 + 涨幅 + 一句话判断”。
- 领跌：从已提供的行业/概念领跌榜中选 1-2 个最弱方向，写“板块 + 跌幅 + 一句话判断”。
- 涨跌幅必须来自输入数据；没有可靠新闻解释原因时，只做盘面判断，不得编造政策、资金或消息原因。
- 关注与结论要同时考虑强势方向的持续性和弱势方向是否扩散。
- 仍保持 900 字以内，禁止 Markdown 表格。
- 信息块顺序固定为：市场、领涨、领跌、消息、关注、风险、结论。

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

    MarketAnalyzer._build_review_prompt = balanced_prompt_builder

    original_inject = MarketAnalyzer._inject_data_into_review

    def balanced_inject(self, review, overview, news=None):
        rendered = original_inject(self, review, overview, news)
        if _is_brief_mode() and getattr(self, "region", "cn") == "cn":
            return _ensure_cn_sector_balance(rendered, overview)
        return rendered

    MarketAnalyzer._inject_data_into_review = balanced_inject
    MarketAnalyzer._opencode_sector_balance_installed = True


def install() -> None:
    """Install extensions after the existing sitecustomize compatibility layer."""
    _install_exa_search_provider()
    _install_balanced_sector_brief_patch()
