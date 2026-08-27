# -*- coding: utf-8 -*-
"""Runtime compatibility tweaks for the GitHub Actions entrypoint.

This file is imported automatically by Python before ``main.py`` runs.  The
patches are deliberately scoped to the daily-analysis entrypoint so that pip,
tests, and library imports keep the repository's normal behavior.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Dict, List


# OpenCode Go exposes MiniMax M3 at
# https://opencode.ai/zen/go/v1/messages.  LiteLLM's Anthropic adapter appends
# /v1/messages to api_base, so the configured base must stop at /zen/go.
_OPENCODE_GO_ANTHROPIC_BASE = "https://opencode.ai/zen/go"
_OPENCODE_GO_MINIMAX_MODEL = "minimax-m3"


def _is_main_entrypoint() -> bool:
    return os.path.basename(sys.argv[0] or "") == "main.py"


def _is_brief_mode() -> bool:
    report_type = (os.getenv("MARKET_REVIEW_REPORT_TYPE") or os.getenv("REPORT_TYPE") or "").strip().lower()
    return report_type in {"simple", "brief"}


def _configure_opencode_go_minimax() -> None:
    """Reuse OPENAI_API_KEY for the user's OpenCode Go MiniMax M3 key.

    The upstream API is Anthropic Messages and authenticates with ``x-api-key``.
    LiteLLM handles that header when the route uses the Anthropic provider.
    """

    model = (os.getenv("OPENAI_MODEL") or "").strip().lower()
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if model != _OPENCODE_GO_MINIMAX_MODEL or not api_key:
        return

    existing_channels = [
        item.strip().lower()
        for item in (os.getenv("LLM_CHANNELS") or "").split(",")
        if item.strip()
    ]
    channels = ["minimax"] + [item for item in existing_channels if item != "minimax"]

    os.environ["LLM_CHANNELS"] = ",".join(channels)
    os.environ["LLM_MINIMAX_PROTOCOL"] = "anthropic"
    os.environ["LLM_MINIMAX_BASE_URL"] = _OPENCODE_GO_ANTHROPIC_BASE
    os.environ["LLM_MINIMAX_API_KEY"] = api_key
    os.environ["LLM_MINIMAX_MODELS"] = _OPENCODE_GO_MINIMAX_MODEL
    os.environ["LLM_MINIMAX_ENABLED"] = "true"
    os.environ["LITELLM_MODEL"] = f"anthropic/{_OPENCODE_GO_MINIMAX_MODEL}"


def _configure_brief_delivery() -> None:
    if not _is_brief_mode():
        return
    # In brief mode, the stock report should be a decision summary rather than
    # a second full analysis document.  The market review is compacted below.
    os.environ["REPORT_SUMMARY_ONLY"] = "true"
    os.environ["REPORT_SHOW_LLM_MODEL"] = "false"
    os.environ["MARKET_REVIEW_REPORT_TYPE"] = "brief"


def _trim_inline(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    cleaned = cleaned.replace("**", "").replace("__", "")
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip("，,；;。 .") + "…"


def _compact_market_text(text: str, max_chars: int = 1150) -> str:
    """Last-resort guardrail for Enterprise WeChat readability.

    The prompt normally keeps the report below this threshold.  This fallback
    prevents a verbose model response from being split into several WeChat
    messages by keeping only the first useful facts from each decision section.
    """

    raw = str(text or "").strip()
    if not raw or len(raw) <= max_chars:
        return raw

    aliases: Dict[str, tuple[str, ...]] = {
        "市场": ("盘面", "市场总结", "market summary", "指数", "indices"),
        "主线": ("板块", "主线", "sector", "theme"),
        "消息": ("消息", "催化", "news", "catalyst"),
        "关注": ("明日", "交易计划", "后市", "outlook", "plan", "watch"),
        "风险": ("风险", "risk"),
        "结论": ("结论", "conclusion"),
    }
    buckets: Dict[str, List[str]] = {key: [] for key in aliases}
    title = ""
    summary = ""
    current = "市场"

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("|") or re.fullmatch(r"[-:| ]+", line):
            continue
        if not title and line.startswith("#"):
            title = _trim_inline(line.lstrip("# "), 70)
            continue
        if not summary and line.startswith(">"):
            summary = _trim_inline(line.lstrip("> "), 150)
            continue

        heading_text = line.lstrip("# ").lower()
        if line.startswith("#"):
            for key, keys in aliases.items():
                if any(token in heading_text for token in keys):
                    current = key
                    break
            continue

        # Already-compact bold labels, e.g. **主线**: ...
        matched_inline = False
        lower_line = line.lower()
        for key, keys in aliases.items():
            if line.startswith(f"**{key}**") or any(
                lower_line.startswith(f"**{token}") for token in keys if token.isascii()
            ):
                buckets[key].append(_trim_inline(line, 190))
                matched_inline = True
                break
        if matched_inline:
            continue

        if len(buckets[current]) < (2 if current in {"主线", "消息", "关注"} else 1):
            cleaned = line.lstrip("-*•0123456789. ")
            if cleaned:
                buckets[current].append(_trim_inline(cleaned, 150))

    lines: List[str] = []
    if title:
        lines.append(f"## {title}")
    if summary:
        lines.append(f"> {summary}")

    for key in ("市场", "主线", "消息", "关注", "风险", "结论"):
        items = buckets[key]
        if not items:
            continue
        if len(items) == 1:
            lines.append(f"**{key}** {items[0]}")
        else:
            lines.append(f"**{key}**")
            lines.extend(f"- {item}" for item in items)

    compact = "\n".join(lines).strip()
    if not compact:
        compact = _trim_inline(raw, max_chars)
    if len(compact) > max_chars:
        compact = compact[: max_chars - 1].rstrip() + "…"
    return compact


def _install_market_review_patch() -> None:
    from src.market_analyzer import MarketAnalyzer

    original_prompt_builder = MarketAnalyzer._build_review_prompt

    def brief_prompt_builder(self, overview, news):
        prompt = original_prompt_builder(self, overview, news)
        if not _is_brief_mode():
            return prompt

        market_name = "美股" if getattr(self, "region", "cn") == "us" else "A股"
        return prompt + f"""

【最终输出覆盖规则｜企业微信群简报】
上面的详细章节仅作为分析素材。最终回答必须改写为一条适合企业微信群快速扫读的{market_name}简报，并覆盖原来的七段长报告格式：
- 总长度控制在 900 个中文字符以内；宁可少写，不要重复解释。
- 禁止 Markdown 表格；禁止长段落；不要复述原始数据两次。
- 标题后最多保留 6 个信息块：市场、主线、消息、关注、风险、结论。
- 市场：主要指数与涨跌家数/成交额（有数据才写），合并成 1-2 行。
- 主线：最多 2 个方向，每个方向只写“表现 + 原因/判断”一句。
- 消息：最多 2 条真正影响盘面的新闻；没有可靠新闻时明确写“暂无可靠增量新闻”，禁止编造。
- 关注：最多 2 个下一交易时段观察点，不写完整交易教程。
- 风险：最多 1 条最重要风险。
- 结论：1 句话给出市场强弱、主线与操作节奏。
- 每个要点尽量不超过 60 个汉字，少量 emoji 即可，不要堆叠 emoji。
- 直接输出简报，不要解释你为何精简。

推荐格式：
## {market_name}简报
> 一句话市场状态
**市场** 指数/情绪/成交核心数据
**主线**
- 方向1：一句话
- 方向2：一句话
**消息**
- 新闻1
- 新闻2
**关注** 观察点1；观察点2
**风险** 一句话
**结论** 一句话
"""

    MarketAnalyzer._build_review_prompt = brief_prompt_builder

    original_generate = MarketAnalyzer._generate_market_review_with_metadata

    def brief_generate(self, prompt, *, provider, model):
        result = original_generate(self, prompt, provider=provider, model=model)
        if _is_brief_mode() and result is not None and getattr(result, "text", None):
            result.text = _compact_market_text(result.text)
        return result

    MarketAnalyzer._generate_market_review_with_metadata = brief_generate


def _install_notification_patch() -> None:
    from src.notification import NotificationService

    original_aggregate = NotificationService.generate_aggregate_report

    def brief_aggregate(self, results, report_type, report_date=None):
        value = getattr(report_type, "value", report_type)
        if str(value or "").strip().lower() in {"simple", "brief"}:
            # This renderer is already purpose-built for Enterprise WeChat and,
            # together with REPORT_SUMMARY_ONLY=true, keeps one stock to one line.
            return self.generate_wechat_summary(results)
        return original_aggregate(self, results, report_type, report_date=report_date)

    NotificationService.generate_aggregate_report = brief_aggregate


if _is_main_entrypoint():
    _configure_opencode_go_minimax()
    _configure_brief_delivery()
    try:
        _install_market_review_patch()
        _install_notification_patch()
    except Exception:
        # sitecustomize must never make the application unstartable.  The normal
        # repository behavior remains available if an upstream refactor changes
        # one of the patched symbols.
        pass
