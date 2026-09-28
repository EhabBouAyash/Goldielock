"""Paper-only T1 / T2 / T3 agents (DeepSeek auditor, not an arbiter).

This package does not connect to a funded account.

## Folders

| Agent | Folder | Instrument | Rule |
|---|---|---|---|
| T1 | `deepseek_trial/t1_gold_trend/` | XAUUSD daily (17:00 ET) | 12-1 return > 0 **and** close > SMA(200); long or cash |
| T2 | `deepseek_trial/t2_gold_compression/` | XAUUSD H1 | Quiet ATR(10) then Donchian-20 close break; both sides; **EXPERIMENTAL** |
| T3 | `deepseek_trial/t3_btc_ma/` | BTCUSD daily UTC | SMA(5) > SMA(100)×1.01; long or cash; never short |

Shared engine, risk, calendar, and DeepSeek client live in `deepseek_trial/` (this directory). Ledgers are written under each agent's `books/` folder.

## Environment

`DEEPSEEK_API_KEY` is loaded from the repo `.env` at the Goldielock root (`python-dotenv`). The auditor model is pinned to `deepseek-v4-pro`.

```
DEEPSEEK_API_KEY=...
```

Protective stops never wait on the model. A mismatch, timeout, or invalid JSON is `NO_TRADE` on the supervised book only.

## Commands

From the repo root:

```bash
python3 -m unittest deepseek_trial.test_trial -v

python3 -m deepseek_trial.t1_gold_trend replay
python3 -m deepseek_trial.t2_gold_compression replay
python3 -m deepseek_trial.t3_btc_ma replay

python3 -m deepseek_trial.t1_gold_trend status
python3 -m deepseek_trial.t1_gold_trend paper --audit

python3 -m deepseek_trial
```

`replay` is the reference book (no API). `paper --audit` / `--audit` asks `deepseek-v4-pro` to copy each OPEN ticket. T2 reports must stay labelled EXPERIMENTAL.

T1 staying cash while gold is below the 200-day is a pass for rule integrity, not a reason to loosen the filter.

At 0.25% of $25,000 the planned cash risk is $62.50. Gold's minimum 0.01 lot is 1 oz, so a 2×ATR daily stop wider than about $62.50 is skipped as `LOT_TOO_SMALL`. That is the spec, not a bug.
