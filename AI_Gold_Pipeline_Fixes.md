# Goldielock — AI-First Pipeline Remediation and Macro-Relevance Specification

**Version:** 2.0  
**Prepared:** October 5, 2026  
**Status:** Implementation specification—not an applied or tested patch  
**Scope:** Existing root Claude/LangGraph analysis and manual-trading pipeline  
**Basis:** Owner-supplied repository audit and code excerpts; source files have not been independently inspected

## 1. Objective

Improve the existing AI-first gold trading system without replacing Claude’s contextual judgment with a rigid indicator strategy.

The system should:

- Interpret macro conditions relative to the proposed trade.
- Evaluate chart structure independently of macro narratives.
- Reconcile agreement and disagreement between macro and price.
- Propose structurally supported trades with at least 2R estimated net reward-to-risk.
- Reject late, stale, malformed or financially impermissible plans.
- Calculate account risk independently of Claude.
- Measure executed outcomes separately from directional accuracy.

### Governing principle

> Claude decides whether an opportunity makes sense. Deterministic software decides whether its data, timing, price, risk and account permissions make it actionable.

### Principal change from version 1

Remove the following fixed hierarchy from the proposed live decision policy:

```text
Macro 50% → 4H 20% → 1H 12% → 15M 10% → 5M 8%
```

Retain it only as a labelled legacy comparison policy during testing.

Do not replace it with another arbitrary percentage allocation.

Macro authority must depend on:

```text
trade horizon
information novelty
expectation surprise
transmission channel
observed market response
persistence
data quality
execution risk
```

All proposed classification rules and thresholds require validation. This document does not establish profitability or validate the reported 55% win rate.

---

## 2. Findings and scope boundaries

### Findings reported by the owner

- The root pipeline is an advisory system, not an execution system.
- Claude receives broad macro and multi-timeframe context.
- Final decisions are prose parsed with regex.
- Most trading restrictions are prompt-level instructions.
- Current account equity comes from a manual ledger.
- Floating P&L normally defaults to zero.
- Technical candles use GC=F rather than broker XAUUSD.
- A volatility gate can prevent Claude from evaluating consolidation.
- Several entry points can run the pipeline.
- Existing accuracy logs are not broker-fill-based performance records.

### Unresolved matters

- The exact account programme, phase and permissions.
- A trustworthy sequence establishing the reported breach.
- Reconciliation of substantial manual equity discontinuities.
- Attribution between model decisions and executed trades.
- Whether the reported win rate measures executed trades or directional comparisons.
- Availability of reusable v2 components elsewhere in the repository.

### Migration scope

Initially remain in:

```text
RESEARCH
PAPER
APPROVED_MANUAL_ADVISORY
```

Do not introduce broker order submission, automatic position management or trade copying as an incidental part of these fixes.

**Manual-mode limitation:** Software can withhold invalid cards and report account changes. It cannot prevent a human from placing an unapproved trade or ignoring an expiry warning.

---

## 3. Revised workflow

### Existing workflow

```text
collect_market_context
  → scout_internet
  → generate_trade_signal
  → persist_results
```

### Proposed workflow

```text
collect_market_context
  → collect_account_and_broker_context
  → validate_and_freeze_snapshot
  → independent_chart_assessment
  → assess_macro_relevance
  → reconcile_and_propose_trade
  → validate_trade_plan
  → publish_and_persist
```

News collection may run before or alongside snapshot construction, provided all evidence is timestamped and the final snapshot is immutable.

### Required separation

Maintain distinct outputs for:

1. Raw observations.
2. Chart assessment.
3. Macro relevance assessment.
4. Claude’s proposed decision.
5. Deterministic validation result.
6. Published advisory.
7. Reported and verified executions.

A persuasive model narrative must never substitute for validation.

---

# 4. Macro-Relevance Engine

## 4.1 Separate three macro roles

An economically important event is not automatically useful directional evidence for a particular trade.

Classify macro information into:

### A. Background regime

Examples of intended use:

- Longer-horizon real-yield conditions.
- Broad policy expectations.
- Slower positioning or flow context.

Background context may influence interpretation. It must not independently force a direction merely because it appears in the prompt.

### B. Directional evidence

Information potentially relevant to price behaviour over the proposed holding period.

It requires an identifiable transmission hypothesis and appropriately timestamped evidence.

### C. Execution risk

Conditions that may make entry or holding unsuitable independently of directional interpretation:

- Applicable event restrictions.
- Active price/spread disruption.
- Inadequate mandatory observations.
- Unreliable executable pricing.

Do not merge these roles into one “macro score.”

Gold’s drivers and their relationships can change over time. The World Gold Council’s attribution framework explicitly investigates those changes, but its weekly/monthly framework does not establish intraday weighting rules. ([gold.org](https://www.gold.org/goldhub/tools/gold-return-attribution-model))

## 4.2 Assess relevance to the proposed trade

For each event or observation, record:

| Dimension | Required assessment |
|---|---|
| Instrument connection | Plausible connection to gold through rates, currency, inflation compensation, risk demand or another identified channel |
| Novelty | New information, revision, repeated report or unchanged condition |
| Surprise | Actual versus trustworthy expectations, where available |
| Horizon relevance | Relationship to the proposed holding period |
| Observed response | Supplied gold, dollar, rates or other relevant observations |
| Persistence | Continuing repricing, fading response or insufficient evidence |
| Contradictions | Observations inconsistent with the proposed interpretation |
| Reliability | Source quality, timestamps, revisions and missing information |
| Execution consequences | Whether the event affects entry eligibility independently of direction |

### Required cautions

- Claude must not invent consensus, surprise or market response.
- Missing response data means **unknown**, not “the market ignored it.”
- Co-movement is descriptive evidence, not proof of causation.
- Related inputs must not be counted as independent votes.
- A repeated headline must not repeatedly increase conviction.
- A high-importance label must not automatically create high trading relevance.

For unscheduled events, leave numeric surprise unavailable unless a defensible measurement exists.

## 4.3 Relevance classifications

Use a small, auditable classification system rather than unsupported numerical precision.

| Classification | Permitted authority |
|---|---|
| `BACKGROUND_ONLY` | Context; cannot independently force a direction or reject an otherwise valid setup |
| `DIRECTIONAL_UNCONFIRMED` | Soft prior with explicit uncertainty |
| `ACTIVE_REPRICING` | Strong directional context supported by supplied observations |
| `EXECUTION_RISK` | May block actionable publication under explicit rules |
| `INSUFFICIENT_EVIDENCE` | Disclose limitation; block only where the missing evidence is mandatory |

These are proposed policy categories—not validated trading probabilities.

An event may have both a background role and execution-risk consequences. Store those separately.

## 4.4 Event lifecycle

Track scheduled events through:

```text
SCHEDULED
  → RELEASED_UNVERIFIED
  → RESPONSE_BUILDING
  → ACTIVE_REPRICING
  → FADING / ABSORBED / UNCERTAIN
```

Other branches include:

```text
CANCELLED
SOURCE_CONFLICT
DATA_UNAVAILABLE
REACTIVATED_BY_NEW_EVIDENCE
```

Do not apply one universal expiry interval to every event.

A headline can become stale while its broader regime remains relevant. Conversely, a recent article can repeat old information.

Reactivation requires new evidence—not another publication of the same story.

## 4.5 Deduplicate by underlying event

Store a canonical `event_id` with supporting articles beneath it.

Multiple reports of one release must not become multiple independent bullish or bearish inputs.

Maintain:

```text
event_id
event_type
scheduled_time
actual_publication_time
supporting_sources
new_information
revision_information
relevance_role
lifecycle_state
```

---

# 5. Independent Chart Assessment

Before reconciling macro narratives, build a chart-state assessment that excludes news commentary.

Use code-generated structure and, where justified, a separate chart-only model assessment.

The report should identify:

- Trend, range or transition.
- Relevant structural boundaries.
- Breakouts, failures, reclaims and retests.
- Whether movement is continuing or being rejected.
- Entry extension and available target room.
- Confirmed versus forming evidence.
- Agreement and disagreement across timeframes.

### Proposed timeframe responsibilities

| Timeframe | Intended role |
|---|---|
| Daily / 4H | Larger structure and major boundaries |
| 1H / 15M | Developing opportunity |
| 5M | Entry confirmation |
| Live quote | Execution eligibility |

These are initial design choices, not fixed importance percentages.

Save the chart assessment before combining it with macro interpretation. This allows later measurement of how macro changed the proposed decision.

---

# 6. Macro/Chart Reconciliation Policy

Claude must explicitly reconcile the two assessments.

## 6.1 Agreement

When relevant macro evidence and chart structure agree:

- Explain the agreement.
- Select a supported setup.
- Apply normal timing, structure, net-RR and risk checks.

Agreement is not permission to increase risk beyond the engine’s allowance.

## 6.2 Weak or background macro evidence

When macro is background-only or weakly relevant:

- Let chart structure carry the directional decision.
- Do not force a macro story for every trade.
- Do not block a valid setup merely because slow macro context points elsewhere.

## 6.3 Material disagreement

When relevant macro interpretation conflicts with price:

Claude must consider:

- Whether the information may already be reflected in prices.
- Whether its expected transmission is unconfirmed.
- Whether another driver appears more relevant.
- Whether the chart move is a failed response or temporary counter-move.
- Whether the evidence is insufficient.

Permitted decisions remain:

```text
PROPOSE_LONG
PROPOSE_SHORT
WAIT
NO_TRADE
```

Do not introduce an automatic rule that disagreement always means no trade.

A counter-narrative trade requires explicit supporting evidence and remains subject to normal validation.

## 6.4 Macro direction without an entry

Strong macro interpretation with no usable chart entry should produce:

```text
WAIT
```

or a time-limited conditional proposal.

It must not produce an invented entry merely to act on a directional opinion.

## 6.5 Execution risk

An applicable execution restriction may block a trade even when macro and chart direction agree.

Keep the explanation separate:

```text
directional thesis: potentially valid
execution eligibility: denied
```

### Illustrative regression scenario

Input:

- Gold-positive macro commentary.
- Continued downside structure.
- Failed rallies.
- No supplied confirmation of the expected bullish transmission.

Expected behaviour:

- No automatic long.
- Explicit discussion of contradictory observations.
- Short, wait or no-trade remains possible.
- The chosen outcome must reference actual evidence.

---

# 7. Revised Claude Prompt Responsibilities

Remove instructions requiring Claude to:

- Calculate authoritative dollar risk.
- Choose authoritative ounces or broker lots.
- Select leverage.
- Override account permissions.
- Apply fixed macro/timeframe percentages.

Instead require:

1. Independent chart interpretation.
2. Relevant macro classification.
3. Supporting and contradictory evidence.
4. Explanation of macro/chart reconciliation.
5. A structurally supported plan or explicit abstention.
6. Conditions that invalidate the thesis.

### Required prompt principles

```text
Economic importance is not the same as trade relevance.

Do not assume all macro inputs have equal authority.

Do not assume related observations are independent votes.

Do not force price to conform to a macro narrative.

Do not ignore relevant execution risk because the chart looks clear.

Use supplied observations; identify unknowns instead of inventing them.

A trade can be valid without a strong macro directional view.

No narrative can override deterministic validation.
```

Retrieved articles are untrusted data, not instructions.

---

# 8. Structured Decision Contract

Replace prose parsing as trading authority.

Claude may propose direction, structure and interpretation. Code determines validated status and sizing.

## Required fields

```text
schema_version
snapshot_id
decision
side
setup_family
proposed_holding_horizon
entry_zone
trigger
stop_reference
target_reference
requested_validity_seconds
invalidation_conditions

chart_assessment_id
macro_assessment_id
macro_role
relevant_event_ids
observed_response_evidence_ids
supporting_evidence_ids
contradicting_evidence_ids
macro_chart_relationship
reconciliation_summary
```

Confidence may be recorded as an assessment label. Do not treat it as a calibrated win probability or use it directly for position sizing.

## Validation requirements

Reject:

- Snapshot mismatches.
- Missing or non-finite prices.
- Invalid stop/entry/target ordering.
- Unknown evidence references.
- Unsupported trigger instructions.
- Contradictory decision fields.
- Missing required reconciliation information.
- Model-supplied sizing that attempts to override code.

Use supported Anthropic structured outputs and local semantic validation. Anthropic documents `output_config.format` and schema limitations; schema-compliant output does not establish financial correctness. ([platform.claude.com](https://platform.claude.com/docs/en/build-with-claude/structured-outputs))

Illustrative modification:

```python
payload["output_config"] = {
    "effort": effort,
    "format": {
        "type": "json_schema",
        "schema": supported_decision_schema,
    },
}
```

Handle refusals, truncation and API failures as non-actionable outcomes.

Derive any legacy `GOLD_SIGNAL` footer from the validated result. Never use it as authority.

Verify the configured model’s availability and capabilities. Do not silently substitute another model.

---

# 9. Market Evidence and Pricing Authority

## 9.1 Broker versus external prices

Actionable publication requires an eligible, fresh broker quote.

External prices remain contextual:

```text
role: external_reference
execution_eligible: false
```

Modify the existing GC=F fallback so it cannot silently become executable XAUUSD.

Record:

```text
source
instrument
observation_time
received_time
bid
ask
last
execution_eligible
is_fallback
```

Remove SSL-verification bypasses from actionable data paths.

## 9.2 Proxy chart limitations

Keep GC=F for research and futures context, but record:

- Instrument identity.
- Basis adjustment and timestamp.
- Basis uncertainty.
- Contract/roll discontinuities.
- Whether levels are broker-derived or proxy-derived.

Do not assume one basis adjustment makes historical proxy structure equivalent to broker structure.

## 9.3 Closed candles

Each bar needs:

```text
open_time
scheduled_close_time
is_closed
source
instrument
```

- Confirmed indicators use completed bars.
- Forming bars are separately labelled.
- Zero volume is not a general completion test.
- Validate indicator warm-up.
- Validate 4H alignment and resampling.

MT5 defines bar index zero as the current bar; ingestion must deliberately distinguish it from completed history. ([mql5.com](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrompos_py))

---

# 10. Macro Data Clocks and Freshness

Every macro observation needs:

```text
observation_time
publication_time
received_time
frequency
source
revision_status
intended_role
```

Examples of roles:

| Input | Intended treatment |
|---|---|
| Real-yield series | Dated regime context unless a genuine intraday source exists |
| DXY / rates observations | Directional evidence only at their actual supplied frequency |
| Scheduled release | Expectations and event-risk context before publication |
| Published release | New information after verified publication |
| TGA / Fed balance sheet | Slower context, not automatically an intraday trigger |
| Positioning / flows | Context with explicit reporting lag |
| Headlines | Evidence requiring novelty, source and event checks |

`DFII10` is a daily series. It must not be presented as a live five-minute real-yield change. ([fred.stlouisfed.org](https://fred.stlouisfed.org/series/DFII10))

Do not:

- Mix daily and intraday observations without labelling the mismatch.
- Compare yield changes and price changes as interchangeable units.
- Insert revised data into an earlier historical snapshot.
- Let missing optional context suppress every trade.

Missing mandatory execution or account information must block actionable publication. Missing optional context should produce a disclosed limitation.

---

# 11. Account State and Risk Enforcement

## 11.1 Read-only broker state

Where permitted, collect:

- Account identity and currency.
- Balance and equity.
- Open positions.
- Pending orders.
- Quotes and symbol specifications.
- Margin information.
- Deal history.

Unknown state remains unknown. A failed connection must not become zero floating P&L or a flat account.

Verify deployment compatibility; the presence of `launchd/` does not prove the MT5 integration can run unchanged in the existing environment.

## 11.2 Explicit account profile

Require:

```text
programme
phase
initial_balance
account_currency
daily_loss_rule
overall_loss_rule
cumulative_risk_rule
server_reset_schedule
internal_reserves
risk_policy_version
advisory_permissions
automation_permissions
```

Do not infer the programme from the existing dollar limits.

## 11.3 Deterministic sizing

Before publishing an actionable card:

1. Verify fresh account state.
2. Calculate daily and overall headroom.
3. Include open-position and pending-order exposure.
4. Apply applicable cumulative-risk rules.
5. Apply internal reserves.
6. Calculate size using broker specifications.
7. Round down to permitted increments.
8. Reject if minimum permitted size exceeds the allowance.

Claude cannot increase the allowance because macro conviction is high.

FundedNext’s funded-stage risk policy includes cumulative-risk limits and possible reclassification. Use the account’s actual applicable parameters. ([help.fundednext.com](https://help.fundednext.com/en/articles/14702245-what-are-the-risk-limits-on-a-fundednext-account))

### Avoid double-counting

Current equity already reflects floating outcomes.

Estimate additional deterioration from current state to existing stops rather than subtracting both floating loss and full original entry-to-stop risk again.

Keep distinct calculations for:

- Firm rule utilisation.
- Economic exposure.
- Loss headroom.
- Internal reserves.

### Daily reset

Implement the configured server schedule, not local computer midnight.

FundedNext’s daily-loss documentation includes floating outcomes and relevant charges and warns that positions carried through the reset can cause a breach. ([help.fundednext.com](https://help.fundednext.com/en/articles/8019811-how-can-i-calculate-the-daily-loss-limit))

---

# 12. Minimum Net 2R and Anti-Late-Entry Controls

## 12.1 Net reward-to-risk

Estimate:

```text
loss_at_stop =
    price-movement loss
    + applicable costs
    + execution reserve

reward_at_target =
    price-movement profit
    - applicable costs
```

Require:

```text
reward_at_target / loss_at_stop >= 2.0
```

Use MT5’s `order_calc_profit()` for account-currency price-movement P&L estimates, with separate costs and reserves. ([mql5.com](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcprofit_py))

A gross 2R target does not automatically satisfy this requirement.

## 12.2 Structural integrity

- Stops and targets reference supplied evidence.
- Do not extend a target merely to rescue a late entry.
- Do not widen a stop to preserve size.
- Proxy-to-broker translation must be explicit.
- Reject unsupported target room.

The minimum is an entry qualification and planned objective—not a guarantee that every positive exit realises 2R.

Emergency exits remain possible and must be recorded at their actual outcome.

## 12.3 Entry boundaries

Before costs:

```text
Long:  entry <= (target + 2*stop) / 3
Short: entry >= (target + 2*stop) / 3
```

Final boundaries must use the net outcome calculation.

## 12.4 Plan lifecycle

```text
PROPOSED
  → VALIDATED_ADVISORY
  → EXPIRED / INVALIDATED / REJECTED
  → EXECUTION_REPORTED
  → EXECUTION_VERIFIED
  → CLOSED
```

Store opportunity, snapshot, decision, publication and expiry times separately.

Validity does not restart when:

- An email arrives.
- A process restarts.
- An API call retries.
- The same hypothesis is reprinted.

Fresh approval requires fresh evidence and an explicit new decision.

Macro changes may invalidate a plan, but only through logged evidence—not a silent reinterpretation of an old narrative.

---

# 13. Reconcile the Historical Ledger

Preserve original manual files. Import corrections as separate records.

Track:

- Trading outcomes.
- Commissions, swaps and fees.
- Deposits, withdrawals and credits.
- Account adjustments, resets and replacements.
- Floating P&L at snapshots.
- Provenance of manual corrections.

Do not equate manual “equity” with reconstructed balance without determining what it represented.

## Required discrepancy fixture

```text
TRADE_018 equity:          29,478.65
TRADE_019 equity:          24,500.15
Reported TRADE_019 P&L:      -137.50
Unexplained residual:       4,841.00
```

Classify as unreconciled.

Do not invent a withdrawal, trading loss or breach to explain it.

Verified current broker state may establish current eligibility even while older history remains quarantined.

---

# 14. Opportunity Coverage and the Volatility Gate

The current consolidation-without-breakout gate may prevent the AI from assessing valid reversal or range opportunities.

Test—not blindly deploy—a variant that:

- Supplies volatility regime as context.
- Allows evaluation of approved trade families.
- Uses the same data, timing, risk and net-RR controls.

Initial research families:

```text
trend pullback continuation
compression breakout/retest
failed breakout/range rejection
post-event continuation
```

Macro relevance should help select the family, not impose one family universally.

Do not force a trade count to meet an income objective. Measure which filters remove opportunities and whether those removals improve realised outcomes.

Record rejection reasons such as:

```text
DATA_UNAVAILABLE
ACCOUNT_UNVERIFIED
MODEL_OUTPUT_INVALID
STRUCTURE_UNSUPPORTED
NET_RR_BELOW_MINIMUM
PLAN_EXPIRED
ENTRY_OUTSIDE_ZONE
EVENT_RESTRICTION
RISK_HEADROOM_INSUFFICIENT
DUPLICATE_OPPORTUNITY
MODEL_WAIT_MACRO_CHART_DIVERGENCE
```

The last reason is a model decision to measure—not an automatic global veto.

---

# 15. Scheduling, Latency and Publication

Use one authoritative publisher per account.

- Agent loop, chat and scheduled monitor cannot publish competing cards.
- Chat must use the same validation path.
- Use locking and atomic updates.
- An older response cannot overwrite newer state.
- Record every run, including blocked and failed runs.

Retries must respect a decision deadline. If retrying consumes validity, stop and save a non-actionable result.

Measure:

```text
collection duration
chart-assessment duration
macro-assessment duration
model duration
validation duration
publication delay
signal-to-reported-fill delay
```

Cache slow context where appropriate, but disclose its age. Do not add so many model stages that relevance analysis creates more late entries.

### Email requirements

Display:

- Plan ID.
- Snapshot time.
- Expiry time.
- Permitted entry zone.
- Validation status.
- Macro role and disagreement summary.
- Revalidation requirement.

An expired card is historical information, not an active recommendation.

---

# 16. File-Level Implementation Map

| File | Required change |
|---|---|
| `day_trading_insights.py` | Revised graph, adaptive macro policy, structured decisions, deadlines and validation |
| `day_trading_insights_chat.py` | Same policy and validator; no independent authoritative publication |
| `generate_insights.py` | Retire regex trading authority; retain compatibility rendering |
| `fetch_market_data.py` | Separate executable broker quotes from contextual references |
| `fetch_intraday.py` | Closed-bar metadata, warm-up checks and independent chart-state report |
| `additional_news.py` | Canonical event IDs, timestamps, deduplication and source/conflict metadata |
| `prop_account_tracker.py` | Broker-state authority, reconciliation and deterministic risk assessment |
| `monitor_trades.py` | Overlap prevention, deadline-aware scheduling and validated outputs |
| `email_notifier.py` | Validity and reconciliation summaries; no raw actionable proposals |
| `token_tracker.py` | Stage latency and operating cost |
| `scripts/`, `launchd/` | Single authoritative service and stale-job prevention |

Proposed supporting modules:

```text
macro_relevance.py
decision_contract.py
trade_validation.py
trade_plan_state.py
broker_account_feed.py
performance_ledger.py
account_profiles/
tests/
```

Search for reusable existing v2 components before creating these modules.

---

# 17. Acceptance Tests

## Macro relevance

- Important but irrelevant news cannot independently force direction.
- Repeated reports do not increase conviction repeatedly.
- Missing response data remains unknown.
- Background-only macro cannot independently reject a valid setup.
- Active execution risk can block publication despite directional agreement.
- Macro/chart disagreement requires explanation.
- Disagreement does not automatically mean no trade.
- Related macro inputs are not counted as independent votes.
- New evidence is required to reactivate an old event.
- No revised or future observation enters an earlier snapshot.

## Decision and data

- Malformed or mismatched output produces no actionable card.
- Unknown evidence references are rejected.
- Claude sizing cannot override the engine.
- GC=F fallback remains non-executable.
- Forming bars cannot appear as confirmed evidence.
- Unknown account state does not become zero exposure.

## Timing and risk

- Stale plans are rejected even if apparent RR remains adequate.
- Entry outside the approved zone is rejected.
- Retries and restarts cannot renew validity.
- Net RR below 2 is rejected.
- Existing exposure affects sizing.
- Floating loss is not double-counted.
- Account reset calculations use the configured schedule.

## Historical fixtures

- Preserve TRADE_024 as a recorded successful manual trade without claiming proven model attribution.
- Flag TRADE_010’s `$0.50` risk field as unreliable.
- Reject TRADE_010’s reported next-day use as expired. Its approximately 2.12R gross ratio does not make it timely.
- Preserve the TRADE_018/019 unexplained residual.
- Do not invent a recorded breach sequence.

## Publication

- Older runs cannot replace newer authoritative output.
- Chat cannot bypass validation.
- Raw model proposals cannot become emailed actionable cards.
- Reported, paper and verified executions remain distinct.

---

# 18. Evaluation and Rollout

## Performance populations

Report separately:

1. Directional comparisons.
2. Raw proposals.
3. Validated advisories.
4. Paper executions.
5. Manual reported executions.
6. Broker-verified executions.

Do not combine them into one win rate.

## Required metrics

- Verified trade/campaign count.
- Average winner and loser in realised R.
- Net expectancy and drawdown.
- Validated and executed opportunities per month.
- Oversized losses.
- Stale and missed entries.
- Account-rule violations and proximity.
- API and operating costs.
- Payout economics where applicable.

## Macro-specific metrics

- Decisions changed by macro.
- Trades rejected because of macro.
- Results by relevance classification.
- Results when macro and chart agree or diverge.
- Results of background-only versus active-repricing decisions.
- Latency added by the macro stage.

## Comparison sequence

### A. Legacy baseline

Replay or inspect archived decisions in paper/research mode.

### B. Safety and timing controls

Apply structured validation, account checks and expiry while preserving the legacy decision policy.

### C. Adaptive macro relevance

Replace fixed weighting without simultaneously changing trade families.

### D. Broader opportunity coverage

Test volatility-gate changes and additional families separately.

Compare directional policies under the **same execution-risk restrictions**. A chart-only comparison must not mean ignoring event-related execution risk.

Freeze model, prompt, schema and policy versions during each evaluation period.

Historical replay requires point-in-time evidence and protection against hindsight. Prospective paper testing remains necessary.

---

# 19. FundedNext Permission Gate

Account permissions must be explicit.

FundedNext’s current CFD policy permits eligible MT4/MT5 bots below `$50,000` with the required fee, while prohibiting bots and automated parameter-management tools on `$50,000`-and-above accounts. External Python is not an exemption. ([help.fundednext.com](https://help.fundednext.com/en/articles/8020763-is-ea-allowed-in-fundednext))

Therefore:

- This migration does not authorise order submission.
- Verify permission for account-connected advisory functionality.
- Do not assume a larger account preserves permissions.
- Do not implement trade replication by default.
- Unknown permissions block account-specific actionable functionality where required.

Keep research capability separate from account execution authority.

---

# 20. Definition of Done

The repaired system:

- Preserves Claude’s contextual judgment.
- Does not give macro a permanent percentage-based veto.
- Distinguishes macro importance, trade relevance and execution risk.
- Assesses chart structure independently before reconciliation.
- Allows justified counter-narrative decisions.
- Does not invent market reactions or causal certainty.
- Rejects stale and late plans.
- Calculates account risk independently.
- Requires structurally supported minimum net 2R at entry.
- Uses trustworthy account state.
- Produces auditable decisions, rejections and executions.
- Makes no unverified profitability claims.

## Final principle
Macro should inform the trade in proportion to its relevance—not dominate because it occupies the largest part of the prompt. Chart direction should not erase real execution risk. Claude reconciles the evidence; deterministic controls protect timing, risk and permissions.

