# -*- coding: utf-8 -*-
"""US sector rankings for brief market reviews.

The upstream market-review pipeline currently has structured sector rankings for
A-shares only. This fork-local extension uses the 11 Select Sector SPDR ETFs as
stable, liquid proxies for S&P 500 sector performance, then injects deterministic
leading/lagging blocks into the US brief report.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

_US_SECTOR_ETFS: Dict[str, str] = {
    "XLK": "科技",
    "XLC": "通信服务",
    "XLY": "可选消费",
    "XLF": "金融",
    "XLI": "工业",
    "XLB": "材料",
    "XLE": "能源",
    "XLP": "必选消费",
    "XLV": "医疗保健",
    "XLU": "公用事业",
    "XLRE": "房地产",
}


def _is_brief_mode() -> bool:
    value = (
        os.getenv("MARKET_REVIEW_REPORT_TYPE")
        or os.getenv("REPORT_TYPE")
        or ""
    ).strip().lower()
    return value in {"simple", "brief"}


def _fetch_us_sector_rankings() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return latest completed-session sector rankings from Select Sector ETFs."""
    try:
        import yfinance as yf

        tickers = list(_US_SECTOR_ETFS)
        frame = yf.download(
            tickers=tickers,
            period="7d",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=True,
            group_by="column",
        )
        if frame is None or frame.empty:
            logger.warning("[USSector] yfinance returned no ETF data")
            return [], []

        try:
            close = frame["Close"]
        except Exception:
            close = None

        rows: List[Dict[str, Any]] = []
        if close is not None:
            for ticker, name in _US_SECTOR_ETFS.items():
                try:
                    series = close[ticker].dropna()
                    if len(series) < 2:
                        continue
                    prev_close = float(series.iloc[-2])
                    last_close = float(series.iloc[-1])
                    if prev_close <= 0:
                        continue
                    change_pct = (last_close / prev_close - 1.0) * 100.0
                    rows.append(
                        {
                            "name": name,
                            "ticker": ticker,
                            "change_pct": change_pct,
                            "current": last_close,
                            "prev_close": prev_close,
                        }
                    )
                except Exception as exc:
                    logger.debug("[USSector] skip %s: %s", ticker, exc)

        if len(rows) < 6:
            logger.warning("[USSector] insufficient ETF rows: %d", len(rows))
            return [], []

        rows.sort(key=lambda item: float(item.get("change_pct", 0.0)), reverse=True)
        top = rows[:5]
        bottom = list(reversed(rows[-5:]))
        logger.info(
            "[USSector] rankings success top=%s bottom=%s",
            [f"{r['name']}({r['ticker']}) {r['change_pct']:+.2f}%" for r in top],
            [f"{r['name']}({r['ticker']}) {r['change_pct']:+.2f}%" for r in bottom],
        )
        return top, bottom
    except Exception as exc:
        logger.warning("[USSector] failed to fetch sector ETFs: %s", exc)
        return [], []


def _format_row(row: Dict[str, Any]) -> str:
    name = str(row.get("name") or "").strip()
    ticker = str(row.get("ticker") or "").strip()
    try:
        pct = float(row.get("change_pct"))
    except (TypeError, ValueError):
        return ""
    if not name:
        return ""
    label = f"{name}({ticker})" if ticker else name
    return f"{label} {pct:+.2f}%"


def _direction_summary(rows: Any, *, direction: str, limit: int = 2) -> str:
    """Render true gainers/losers; fall back to relative strength truthfully."""
    if not isinstance(rows, list) or not rows:
        return ""

    normalized = [row for row in rows if isinstance(row, dict)]
    if direction == "up":
        selected = [row for row in normalized if float(row.get("change_pct", 0.0)) > 0][:limit]
        if selected:
            return "；".join(filter(None, (_format_row(row) for row in selected)))
        fallback = _format_row(normalized[0])
        return f"无行业ETF收涨；相对抗跌 {fallback}" if fallback else ""

    selected = [row for row in normalized if float(row.get("change_pct", 0.0)) < 0][:limit]
    if selected:
        return "；".join(filter(None, (_format_row(row) for row in selected)))
    fallback = _format_row(normalized[0])
    return f"无行业ETF收跌；相对滞涨 {fallback}" if fallback else ""


def _ranking_list(rows: Any, limit: int = 5) -> str:
    if not isinstance(rows, list):
        return ""
    return "；".join(filter(None, (_format_row(row) for row in rows[:limit] if isinstance(row, dict))))


def _insert_before_tail(text: str, block: str) -> str:
    if not block:
        return text
    for marker in ("\n**消息**", "\n**关注**", "\n**风险**", "\n**结论**"):
        pos = text.find(marker)
        if pos >= 0:
            return text[:pos].rstrip() + "\n" + block + "\n" + text[pos:].lstrip("\n")
    return text.rstrip() + "\n" + block


def _replace_block(text: str, label: str, body: str) -> str:
    if not body:
        return text
    pattern = re.compile(
        rf"\*\*{re.escape(label)}\*\*[\s\S]*?(?=\n\s*\*\*(?:领涨|领跌|主线|消息|关注|风险|结论)\*\*|$)"
    )
    replacement = f"**{label}** {body}"
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    return _insert_before_tail(text, replacement)


def _ensure_us_sector_blocks(text: str, overview: Any) -> str:
    report = str(text or "").strip()
    if not report:
        return report

    top = getattr(overview, "top_sectors", None) or []
    bottom = getattr(overview, "bottom_sectors", None) or []
    if not top or not bottom:
        return report

    if "**主线**" in report and "**领涨**" not in report:
        report = report.replace("**主线**", "**领涨**", 1)

    top_text = _direction_summary(top, direction="up")
    bottom_text = _direction_summary(bottom, direction="down")
    if top_text:
        report = _replace_block(
            report,
            "领涨",
            f"{top_text}。基于 Sector SPDR ETF 当日表现，关注强势能否延续。",
        )
    if bottom_text:
        report = _replace_block(
            report,
            "领跌",
            f"{bottom_text}。基于 Sector SPDR ETF 当日表现，关注弱势是否扩散。",
        )
    return report


def install() -> None:
    from src.market_analyzer import MarketAnalyzer

    if getattr(MarketAnalyzer, "_us_sector_extension_installed", False):
        return

    original_overview = MarketAnalyzer.get_market_overview

    def patched_overview(self):
        overview = original_overview(self)
        if getattr(self, "region", "") == "us":
            top, bottom = _fetch_us_sector_rankings()
            if top or bottom:
                overview.top_sectors = top
                overview.bottom_sectors = bottom
        return overview

    MarketAnalyzer.get_market_overview = patched_overview

    original_prompt = MarketAnalyzer._build_review_prompt

    def patched_prompt(self, overview, news):
        prompt = original_prompt(self, overview, news)
        if not _is_brief_mode() or getattr(self, "region", "") != "us":
            return prompt

        top_text = _ranking_list(getattr(overview, "top_sectors", None) or [], limit=5)
        bottom_text = _ranking_list(getattr(overview, "bottom_sectors", None) or [], limit=5)
        if not top_text or not bottom_text:
            return prompt

        return prompt + f"""

【美股板块双向覆盖规则｜优先级高于上面的简报格式】
板块行情使用 11 只 Select Sector SPDR ETF 作为 S&P 500 GICS 行业代理，不得描述成官方行业指数。
结构化板块数据：
- 相对强弱排序前列：{top_text}
- 相对强弱排序后列：{bottom_text}

最终美股 brief 必须同时包含“领涨”和“领跌”：
- “领涨”只列涨跌幅 > 0 的板块；若全部收跌，明确写“无行业ETF收涨”，并给出相对抗跌板块。
- “领跌”只列涨跌幅 < 0 的板块；若全部收涨，明确写“无行业ETF收跌”，并给出相对滞涨板块。
- 各侧最多 1-2 个，名称、ETF 代码、涨跌幅必须严格采用上面的结构化数据。
- 板块涨跌原因只有可靠新闻明确支持时才可写；否则只描述相对强弱与持续性观察。
- 不得根据常识臆测“资金轮动、降息交易、油价驱动”等原因。
- 顺序固定：市场、领涨、领跌、消息、关注、风险、结论。
- 保持短简报，禁止 Markdown 表格。
"""

    MarketAnalyzer._build_review_prompt = patched_prompt

    original_inject = MarketAnalyzer._inject_data_into_review

    def patched_inject(self, review, overview, news=None):
        rendered = original_inject(self, review, overview, news)
        if _is_brief_mode() and getattr(self, "region", "") == "us":
            return _ensure_us_sector_blocks(rendered, overview)
        return rendered

    MarketAnalyzer._inject_data_into_review = patched_inject
    MarketAnalyzer._us_sector_extension_installed = True
