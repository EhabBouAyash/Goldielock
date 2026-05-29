"""Shared system-prompt rule sets for Opus signal generation."""

from pathlib import Path

RULES_DIR = Path(__file__).resolve().parent / "rules"
DOCS_DIR = Path(__file__).resolve().parent / "docs"

ROLE_PREAMBLE = (
    "You are an intraday XAUUSD spot gold trader on a FundedNext Stellar 1-Step $25K CFD account. "
    "Use TOP-DOWN scoring: Macro 50% → 4H 20% → 1H 12% → 15M 10% → 5M 8%. "
    "XAUUSD spot only for decisions, entries, stops, targets, sizing, and logging — no futures symbols. "
    "Apply rules as IF/THEN conditionals; first matching override wins unless a later active RULE says otherwise."
)


def load_rule_file(filename: str) -> str:
    path = RULES_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return f"(Missing rule file: {filename})"


def _section(tag: str, *filenames: str) -> str:
    body = "\n\n".join(load_rule_file(name) for name in filenames)
    return f"<{tag}>\n{body}\n</{tag}>"


def _build_core_prompt(*, sizing_file: str) -> str:
  sections = [
      ROLE_PREAMBLE,
      _section("shared_rules", "shared-signal-rules.rules"),
      _section("macro_rules", "macro-signal-rules.rules"),
      _section(
          "technical_rules",
          "technical-signal-rules.rules",
          sizing_file,
          "output-format.rules",
      ),
      _section("override_conditions", "override-signal-rules.rules"),
      _section("closure_duration_risk_gate", "closure-duration-risk-gate.rules"),
      _section("active_rules", "active-rules.rules"),
  ]
  return "\n\n".join(sections)


def build_day_trading_system_prompt() -> str:
    return _build_core_prompt(sizing_file="sizing-100x.rules")


def build_day_trading_system_prompt_chat() -> str:
    return _build_core_prompt(sizing_file="sizing-50x.rules")


def build_disruption_scout_system_prompt() -> str:
    return (
        build_day_trading_system_prompt()
        + "\n\n"
        + _section("disruption_scout_rules", "disruption-scout.rules")
    )


def build_disruption_scout_system_prompt_chat() -> str:
    return (
        build_day_trading_system_prompt_chat()
        + "\n\n"
        + _section("disruption_scout_rules", "disruption-scout.rules")
    )


DAY_TRADING_SYSTEM_PROMPT = build_day_trading_system_prompt()
DISRUPTION_SCOUT_SYSTEM_PROMPT = build_disruption_scout_system_prompt()
DAY_TRADING_SYSTEM_PROMPT_CHAT = build_day_trading_system_prompt_chat()
DISRUPTION_SCOUT_SYSTEM_PROMPT_CHAT = build_disruption_scout_system_prompt_chat()
