# Trading Rules — Rationale & Human Workflow

This document holds **explanation and workflow** for the trading rule system. Executable
IF/THEN rules live in `rules/*.rules` and are loaded into the Opus system prompt each cycle.

---

## Top-down scoring (why)

Macro 50% → 4H 20% → 1H 12% → 15M 10% → 5M 8%. Macro sets bias; price action can veto.
Prop-firm limits always override signal quality regardless of confluence.

---

## Closure-duration risk gate (rationale)

Closures are not equal risk events. A 1-hour settlement pause and a 48-hour weekend close
both stop trading, but only the longer window lets macro data, headlines, and catalysts
compound with no ability to price them in until reopen. **Risk scales with hours of
no-reaction-time**, not the binary fact of closure.

| Closure type | Typical duration | News-accumulation risk |
|---|---|---|
| Daily rollover / settlement | ~1 hr | LOW |
| Session handoff | Continuous | LOW |
| Weekend | ~48 hrs | HIGH |
| Holiday | 48–96+ hrs | HIGHEST |

**Stress-test question:** If each unresolved catalyst resolves the opposite way, does the
thesis still hold? A live two-way catalyst that would flip the thesis is a conviction
downgrade, not a footnote. Stops assume continuous liquidity; gaps can print through.

---

## Trade post-mortem workflow (human — not loaded to Opus at signal time)

Run on every **closed** trade. Wins get Step 2 only; losses get the full flow.

### Step 1 — Outcome logging (your responsibility)

At trade close, capture immutably:

- Original signal: reasoning, gate/tier results, conviction, entry/stop/targets, size, CLOSURE RISK line if any
- Actual outcome: fill, exit reason, realized P&L, price path entry→exit
- Timestamps of material news/data between entry and exit

**Where to log:**

| Data | Where |
|---|---|
| Execution, P&L, equity, sizing | `trade_backlog.txt` (required) |
| Post-mortem notes, attribution verdict | `trade_backlog.txt` NOTES column |
| Full signal text at entry | `day_trading_insights.txt` (auto-saved by agent) |
| Hardened rules from controllable misses | Append to `rules/active-rules.rules` |

You do **not** need separate logging files beyond `trade_backlog.txt` for normal operation.
`model_trade_log.txt` and `signal_log.csv` are auto-maintained by the agent when using
`--option trading` — reference only, not account truth.

### Step 2 — Attribution test

> Was the uncertainty that caused this loss something the framework could have priced in,
> even without knowing the specific content?

| | Controllable | Exogenous |
|---|---|---|
| Definition | Risk window / missing data / flawed assumption was knowable in advance | No reasonable foresight for timing or nature of move |
| Example | Entering 2h before weekend close with flagged two-way catalysts | Unscheduled tweet with zero precedent |
| Verdict | → Step 3 (may become rule) | → Log as tail risk; no framework change |

When unsure, default **exogenous** — false-positive lessons corrupt the rule set.

### Step 3 — Severity (controllable only)

- **Structural / logical gap** — deterministic checklist flaw; fix after one occurrence
- **Statistical / pattern-based** — requires N≥5 occurrences before hardening

### Step 4 — Rule encoding

Append to `rules/active-rules.rules` in this format:

```
RULE ###:
  IF [trigger]
  THEN [action]
  SOURCE: [trade id]
  TYPE: structural | statistical
  DATE_ADDED: YYYY-MM-DD
```

### Step 5 — Persistence

Rules in `active-rules.rules` are loaded every signal cycle. A post-mortem that does not
produce an appended rule (when controllable) or a NOTES entry (when exogenous) did not
happen for the next session.

### Step 6 — Periodic review

Monthly: retire rules made obsolete by market structure changes.

### Anti-patterns

- Hardening from a single exogenous shock
- Treating statistical hunch as structural certainty before N≥5
- Writing lessons only in chat, not in `active-rules.rules` or NOTES
- Crediting wins to flawed process
