# -*- coding: utf-8 -*-
"""Final compatibility shim for Exa's keyless hosted MCP search.

The public ``web_search_exa`` tool returns text blocks with ``Published:``
metadata.  This provider parses that real date so the repository's existing
freshness filter can keep recent news instead of dropping it as undated.
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
_PROVIDER_NAME = "ExaMCP"


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


def _parse_envelope(body: str) -> Optional[Dict[str, Any]]:
    direct = _json_object(str(body or "").strip())
    if direct is not None:
        return direct

    candidates: List[Dict[str, Any]] = []
    for line in str(body or "").splitlines():
        if not line.startswith("data:"):
            continue
        parsed = _json_object(line[5:].strip())
        if parsed is not None:
            candidates.append(parsed)
    for item in reversed(candidates):
        if "result" in item or "error" in item:
            return item
    return candidates[-1] if candidates else None


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
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    matches = list(re.finditer(r"(?m)^Title:\s*([^\n]+)", normalized))
    rows: List[Dict[str, Any]] = []

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        section = normalized[match.start():end]

        def field(*labels: str) -> Optional[str]:
            for label in labels:
                found = re.search(rf"(?m)^{re.escape(label)}:\s*([^\n]*)", section)
                if found and found.group(1).strip():
                    return found.group(1).strip()
            return None

        highlights = re.search(r"(?ms)^Highlights:\s*(.*)$", section)
        rows.append(
            {
                "title": match.group(1).strip(),
                "url": field("URL"),
                "published": field("Published", "Published Date"),
                "text": highlights.group(1).strip() if highlights else "",
            }
        )
    return rows


def _published_date(item: Dict[str, Any]) -> Optional[str]:
    raw = (
        item.get("published")
        or item.get("publishedDate")
        or item.get("published_date")
        or item.get("publishedAt")
        or item.get("published_at")
        or item.get("date")
    )
    text = str(raw or "").strip()
    if not text or text.upper() == "N/A":
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return match.group(0) if match else None


def _source(url: str) -> str:
    try:
        return urlparse(url).netloc.removeprefix("www.") or "Exa"
    except Exception:
        return "Exa"


def install() -> None:
    if (os.getenv("EXA_MCP_SEARCH_ENABLED") or "true").strip().lower() in {
        "0", "false", "no", "off", "disabled"
    }:
        return

    from src.search_service import BaseSearchProvider, SearchResponse, SearchResult, SearchService

    if getattr(SearchService, "_opencode_public_exa_fix_installed", False):
        return

    class PublicExaMcpProvider(BaseSearchProvider):
        def __init__(self) -> None:
            super().__init__([], _PROVIDER_NAME)

        @property
        def is_available(self) -> bool:
            return True

        def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
            return self.search(query, max_results=max_results, days=days)

        def search(self, query: str, max_results: int = 5, days: int = 7, **_kwargs: Any) -> SearchResponse:
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

            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "web_search_exa",
                    "arguments": {
                        "query": search_query,
                        "numResults": requested,
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
                response = requests.post(_EXA_MCP_URL, headers=headers, json=body, timeout=20)
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

                envelope = _parse_envelope(response.text)
                if envelope is None:
                    raise RuntimeError("Exa MCP 响应无法解析")
                error = envelope.get("error")
                if error:
                    raise RuntimeError(str(error))

                rows = _parse_text_results(_collect_text(envelope.get("result", envelope)))
                results: List[SearchResult] = []
                for row in rows:
                    url = str(row.get("url") or "").strip()
                    if not url:
                        continue
                    snippet = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()[:700]
                    results.append(
                        SearchResult(
                            title=str(row.get("title") or url).strip()[:180],
                            snippet=snippet,
                            url=url,
                            source=_source(url),
                            published_date=_published_date(row),
                        )
                    )
                    if len(results) >= requested:
                        break

                dated = sum(1 for item in results if item.published_date)
                logger.info(
                    "[%s] 搜索 '%s' 完成，返回 %s 条（有日期 %s 条），耗时 %.2fs%s",
                    self.name,
                    search_query,
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
        # Replace the earlier fork-local Exa provider while leaving all upstream
        # fallbacks in their original order behind it.
        self._providers = [
            provider for provider in self._providers
            if getattr(provider, "name", "") != _PROVIDER_NAME
        ]
        self._providers.insert(0, PublicExaMcpProvider())
        logger.info("已启用 Exa MCP 实时新闻搜索，SearXNG/其他引擎作为后续兜底")

    SearchService.__init__ = patched_init
    SearchService._opencode_public_exa_fix_installed = True
