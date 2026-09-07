# -*- coding: utf-8 -*-
"""Fork-local reliability fixes for the production market-review workflow.

This module intentionally keeps provider-specific compatibility behavior out of
upstream modules so the fork remains easy to rebase.

Fixes:
1. OpenCode Go now requires ``x-opencode-session``. Inject a stable per-run
   session header (and a specific User-Agent) for requests routed to
   ``opencode.ai/zen/go``.
2. Market-review news uses the pseudo target ``market``. Sector/macro news is
   valid first-class evidence for that target, so promote it to direct news and
   stop needlessly falling through from Exa to unreliable public SearXNG.
3. Eastmoney concept ranking is intermittently unavailable from GitHub-hosted
   runners. Prefer AkShare's Tonghuashun concept-fund-flow endpoint on Actions,
   with the original Eastmoney implementation retained as fallback.
"""

from __future__ import annotations

import functools
import logging
import os
from typing import Any, Dict, Optional, Tuple, List


logger = logging.getLogger(__name__)
_INSTALLED = False


def _opencode_session_id() -> str:
    explicit = (os.getenv("OPENCODE_SESSION_ID") or "").strip()
    if explicit:
        return explicit

    repository = (os.getenv("GITHUB_REPOSITORY") or "daily-stock-analysis").strip()
    run_id = (os.getenv("GITHUB_RUN_ID") or "local").strip()
    attempt = (os.getenv("GITHUB_RUN_ATTEMPT") or "1").strip()
    # Stable within one workflow attempt, different across runs. This is the
    # granularity OpenCode Go needs for prompt-cache routing.
    return f"dsa:{repository}:{run_id}:{attempt}"


def _is_opencode_go_request(kwargs: Dict[str, Any]) -> bool:
    candidates = (
        kwargs.get("api_base"),
        kwargs.get("base_url"),
        os.getenv("OPENAI_BASE_URL"),
        os.getenv("LLM_OPENAI_BASE_URL"),
        os.getenv("LLM_ANTHROPIC_BASE_URL"),
    )
    return any("opencode.ai/zen/go" in str(value or "").lower() for value in candidates)


def _inject_opencode_headers(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    if not _is_opencode_go_request(kwargs):
        return kwargs

    updated = dict(kwargs)
    raw_headers = updated.get("extra_headers")
    headers: Dict[str, str] = dict(raw_headers) if isinstance(raw_headers, dict) else {}
    headers.setdefault("x-opencode-session", _opencode_session_id())
    # OpenCode Go asks clients to identify themselves rather than using a broad
    # browser/SDK User-Agent. Do not include secrets in either header.
    headers.setdefault("User-Agent", "daily-stock-analysis/github-actions")
    updated["extra_headers"] = headers
    return updated


def _install_opencode_go_headers() -> None:
    """Inject required headers at config and transport layers.

    The config wrapper covers this repository's normal direct LiteLLM path.
    The LiteLLM wrappers are a defensive fallback for Router/alternate paths.
    """
    try:
        import src.config as config_module

        original_extra = config_module.extra_litellm_params
        if not getattr(original_extra, "_dsa_opencode_header_patch", False):
            @functools.wraps(original_extra)
            def patched_extra(model: str, config: Any) -> Dict[str, Any]:
                params = dict(original_extra(model, config) or {})
                return _inject_opencode_headers(params)

            patched_extra._dsa_opencode_header_patch = True  # type: ignore[attr-defined]
            config_module.extra_litellm_params = patched_extra
    except Exception as exc:
        logger.warning("OpenCode Go config header patch skipped: %s", exc)

    try:
        import litellm

        original_completion = litellm.completion
        if not getattr(original_completion, "_dsa_opencode_header_patch", False):
            @functools.wraps(original_completion)
            def patched_completion(*args: Any, **kwargs: Any) -> Any:
                return original_completion(*args, **_inject_opencode_headers(kwargs))

            patched_completion._dsa_opencode_header_patch = True  # type: ignore[attr-defined]
            litellm.completion = patched_completion

        original_acompletion = getattr(litellm, "acompletion", None)
        if original_acompletion and not getattr(original_acompletion, "_dsa_opencode_header_patch", False):
            @functools.wraps(original_acompletion)
            async def patched_acompletion(*args: Any, **kwargs: Any) -> Any:
                return await original_acompletion(*args, **_inject_opencode_headers(kwargs))

            patched_acompletion._dsa_opencode_header_patch = True  # type: ignore[attr-defined]
            litellm.acompletion = patched_acompletion

        router_cls = getattr(litellm, "Router", None)
        if router_cls is not None:
            original_router_completion = router_cls.completion
            if not getattr(original_router_completion, "_dsa_opencode_header_patch", False):
                @functools.wraps(original_router_completion)
                def patched_router_completion(self: Any, *args: Any, **kwargs: Any) -> Any:
                    return original_router_completion(self, *args, **_inject_opencode_headers(kwargs))

                patched_router_completion._dsa_opencode_header_patch = True  # type: ignore[attr-defined]
                router_cls.completion = patched_router_completion

        logger.info("已启用 OpenCode Go session header 兼容补丁")
    except Exception as exc:
        logger.warning("OpenCode Go transport header patch skipped: %s", exc)


def _install_market_news_relevance_fix() -> None:
    """Treat sector/macro items as first-class evidence for market reviews."""
    try:
        from src.search_service import SearchService

        original_rank = SearchService._rank_news_response
        if getattr(original_rank, "_dsa_market_news_patch", False):
            return

        @classmethod
        def patched_rank(
            cls: Any,
            response: Any,
            *,
            stock_code: str,
            stock_name: str,
            prefer_chinese: bool,
            max_results: int,
            log_scope: str,
        ) -> Any:
            ranked = original_rank(
                response,
                stock_code=stock_code,
                stock_name=stock_name,
                prefer_chinese=prefer_chinese,
                max_results=max_results,
                log_scope=log_scope,
            )
            if str(stock_code or "").strip().lower() != "market" or not getattr(ranked, "results", None):
                return ranked

            promoted = 0
            for item in ranked.results:
                category = getattr(item, "relevance_category", None)
                if category not in {cls._SECTOR_NEWS_CATEGORY, cls._MACRO_NEWS_CATEGORY}:
                    continue
                item.relevance_category = cls._DIRECT_NEWS_CATEGORY
                item.relevance_score = max(int(getattr(item, "relevance_score", 0) or 0), 60)
                reasons = list(getattr(item, "relevance_reasons", None) or [])
                reasons.append("大盘复盘接受板块/宏观市场新闻")
                item.relevance_reasons = reasons
                promoted += 1

            if promoted:
                logger.info("[大盘新闻] 将 %s 条板块/宏观新闻提升为有效大盘新闻，避免无效搜索降级", promoted)
            return ranked

        patched_rank._dsa_market_news_patch = True  # type: ignore[attr-defined]
        SearchService._rank_news_response = patched_rank
    except Exception as exc:
        logger.warning("Market news relevance patch skipped: %s", exc)


def _rank_concept_frame(df: Any, n: int) -> Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
    if df is None or getattr(df, "empty", True):
        return None

    import pandas as pd

    name_candidates = ("行业", "概念名称", "板块名称")
    change_candidates = ("行业-涨跌幅", "涨跌幅", "涨跌幅(%)")
    name_col = next((col for col in name_candidates if col in df.columns), None)
    change_col = next((col for col in change_candidates if col in df.columns), None)
    if not name_col or not change_col:
        return None

    normalized = df.copy()
    normalized[change_col] = pd.to_numeric(normalized[change_col], errors="coerce")
    normalized = normalized.dropna(subset=[change_col])
    if normalized.empty:
        return None

    count = max(1, int(n or 5))
    top = normalized.nlargest(count, change_col)
    bottom = normalized.nsmallest(count, change_col)

    def rows(frame: Any) -> List[Dict[str, Any]]:
        return [
            {"name": str(row[name_col]), "change_pct": float(row[change_col])}
            for _, row in frame.iterrows()
        ]

    return rows(top), rows(bottom)


def _install_concept_ranking_fallback() -> None:
    """Prefer a non-Eastmoney concept ranking on GitHub-hosted runners."""
    try:
        from data_provider.akshare_fetcher import AkshareFetcher

        original = AkshareFetcher.get_concept_rankings
        if getattr(original, "_dsa_concept_fallback_patch", False):
            return

        @functools.wraps(original)
        def patched(self: Any, n: int = 5) -> Any:
            # GitHub-hosted runner IPs are frequently rejected by Eastmoney's
            # push2 endpoint. Tonghuashun's concept fund-flow feed exposes both
            # concept name and current percentage move and is a suitable ranking
            # source for the concise market review.
            if os.getenv("GITHUB_ACTIONS") == "true":
                try:
                    import akshare as ak

                    self._set_random_user_agent()
                    self._enforce_rate_limit()
                    logger.info("[API调用] ak.stock_fund_flow_concept(symbol='即时') 获取概念排行(同花顺)...")
                    frame = ak.stock_fund_flow_concept(symbol="即时")
                    ranked = _rank_concept_frame(frame, n)
                    if ranked:
                        logger.info("[Akshare] 同花顺概念排行获取成功")
                        return ranked
                except Exception as exc:
                    logger.warning("[Akshare] 同花顺概念排行失败: %s，回退原东财接口", exc)

            return original(self, n)

        patched._dsa_concept_fallback_patch = True  # type: ignore[attr-defined]
        AkshareFetcher.get_concept_rankings = patched
    except Exception as exc:
        logger.warning("Concept ranking fallback patch skipped: %s", exc)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    _install_opencode_go_headers()
    _install_market_news_relevance_fix()
    _install_concept_ranking_fallback()
