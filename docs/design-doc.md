# DESIGN DOC — "Ladder" Gold Signal Strategy (LOCKED)

**Status: LOCKED 2026-06-30.**
This document specifies the complete, reproducible definition of the strategy, the
research pipeline that produced its numbers, the exact expected results (checkpoints),
and the deployment architecture. A competent agent following this doc against the
preserved data files MUST reproduce the numbers in §9 exactly.

---

## 0. One-paragraph summary

We follow XAUUSD (gold) signals posted by two public Telegram channels
(**GARY GOLD TRADER** `@Gary_TheTrader`, **GoldScalperNinja** `@GoldScalperNinja`),
but trade **only their SELL signals**, only **fresh** ones (no "again/more" add-ons),
only with a stop-distance of **40–120 pips**, **one trade at a time** (first-come-
first-served), exiting **100% at the first take-profit** (Strategy A). Position risk
follows a **loss-responsive ladder: 1.75% → 1.25% → 1.0%** of account per trade
(down one rung after each losing trade, reset to 1.75% after any winning trade), with
a **2-consecutive-loss daily stop** (day resets 21:00 UTC). Purpose: pass a
GoatFundedTrader (GFT) 2-phase evaluation (+8% then +6%, −4% daily / −10% static
limits). Backtested result (last-1-year data, out-of-sample window): **100% of
evaluation starts pass, median 30 trading days for both phases**.

⚠️ This is a **regime-fitted** edge (see §10 — mandatory reading). It lost money in
2024. A kill-switch is part of the locked design, not optional.

---

## 1. Locked strategy parameters (normative)

| Parameter | LOCKED value |
|---|---|
| Instrument | XAUUSD only |
| Signal sources | `@Gary_TheTrader` + `@GoldScalperNinja` (both, merged) |
| Direction filter | **SELL signals only** (all buys skipped) |
| Add-on filter | **fresh only** — skip signals whose text matches the add-on regex (§3) |
| SL-distance filter | take only if `40 ≤ sl_pips ≤ 120` (pip = $0.10; distance measured from actual fill price, §4) |
| Session / trend / volatility filters | **none** (all hours, no trend gate) |
| Concurrency | **1 position max**, first-come-first-served; while a position is open, all new signals are dropped (never queued) |
| Entry | market, at the open of the first 1-min candle after the signal timestamp |
| Exit | **Strategy A**: single TP at the signal's nearest take-profit; single SL at the signal's stop. No breakeven moves, no trailing, no partials, no time exit |
| Risk ladder | rungs **[1.75%, 1.25%, 1.0%]** of account. Start at rung 0. After a trade with R ≤ 0 → move down one rung (floor at last rung). After a trade with R > 0 → reset to rung 0. State persists across days and across evaluation phases |
| Daily stop | after **2 consecutive losing trades within one trading day**, take no more trades that day (a win resets the intraday streak). Trading day = 21:00 UTC → 21:00 UTC |
| Costs modeled | 3 pips round-trip ($0.30 price), 0 slippage (stress-tested at 4 pips + 0.5 slippage) |
| Prop rules assumed | Phase 1 target +8%, Phase 2 target +6%, firm daily limit −4% (intraday, resets 21:00 UTC), firm max drawdown −10% **static** from phase-start balance, additive (non-compounding) accounting within each phase, no time limit |
| Aggressive alternative (NOT primary) | ladder [2.0%, 1.5%, 1.0%] — faster (med 25d OOS) but 91% full-year / 69% stressed; only if a ~1-in-10 chop-regime failure is acceptable |
| Kill-switch (mandatory) | stop trading if rolling-60-trade net R ≤ −10R, OR after 2 consecutive losing calendar months. Manual review before restart |

Compliance context: GFT support confirmed in writing (2026-06-29) that using public
signal ideas as entry triggers for a self-coded EA is allowed, provided the logic is
our own, fixed-risk (no martingale — this ladder *decreases* after losses), no
hedging (impossible with 1-at-a-time + sell-only), one account only, same EA in eval
and funded. Keep the support ticket as proof.

---

## 2. Data inventory (preserve these files — they define the numbers)

All under `/mnt/c/trading_project/telegram_signals/`. `data/` is **gitignored** —
back these up separately; without them exact reproduction is impossible (a re-fetch
from Telegram would include newer messages and shift every number).

| File | What | Provenance |
|---|---|---|
| `data/raw_gary_max.jsonl` | 17,030 text msgs, 2022-09-04 → 2026-06-29 | `fetch_channel.py https://t.me/Gary_TheTrader gary_max 48` |
| `data/raw_gsn_max.jsonl` | 20,017 text msgs, 2023-05-31 → 2026-06-29 | `fetch_channel.py https://t.me/GoldScalperNinja gsn_max 48` |
| `data/signals_gary_3y.jsonl` | 2,576 parsed signals (2,411 with TP) | parser §3 applied to raw, cutoff ≥ 2023-06-29 |
| `data/signals_gsn_3y.jsonl` | 2,003 parsed signals (1,983 with TP) | parser §3 |
| `data/prices_raw/*.json` | per-day 1-min XAU/USD candles, TwelveData, UTC | `fetch_prices_3y.py` (free key) |
| `data/xauusd_1m_3y.csv` | **1,114,084 bars**, 2023-06-29 → 2026-06-29 15:21 UTC | merged from prices_raw |

Known data quirks (accepted): 2024-05-30 and 2024-05-31 missing (fetch failures,
outside the 1-year study window); final day partial (fetched 15:21 UTC); Saturdays
absent (market closed).

Study windows (hard-coded in `research/sweep_lib.py`):
- `DATA_START = 2025-07-01` — the search uses ONLY signals from here on
- `TRAIN_END = 2026-03-01` — train = Jul-25…Feb-26 (filters selected here);
  **test/OOS = Mar-26…Jun-26** (never used for selection)

---

## 3. Signal parsing (exact rules)

Two per-channel parsers produce one common schema:
`{date, side, addon, entry_low, entry_high, entry_mid, sl, tp_mode, tp_raw, tp_prices, raw, msg_id, channel}`.

### 3.1 Gary (`parse_signals.py`)
- Candidate iff text matches `gold\s+(buy|sell)\b` (case-insens.) AND contains `\bsl\b`.
- Entry: `gold\s+(?:buy|sell)[^\n]*?@?\s*\.?\s*(\d{4,5}(\.\d+)?)(\s*-\s*(\d{4,5}(\.\d+)?))?`
  → one price or a range; `entry_mid = (low+high)/2`.
- SL: `\bsl\b\s*[:\-]?\s*(\d{4,5}(\.\d+)?)` (absent e.g. "Sl: in LiveTrade" → signal dropped later).
- **TP (critical fix):** `\btp\s*\d?\s*[:\-]?\s*((\d{2,5}(\.\d+)?)(\s*/\s*\d{2,5}(\.\d+)?)*)`
  applied with `finditer` over the whole text — this catches BOTH `TP: 50/100Pips`
  and numbered `Tp1: 5150 \n Tp2: 5144` lines (without this, 85% of Gary's signals
  lose their TPs). Collect all numbers, dedupe preserving order.
  - If `max(nums) < 1000` → `tp_mode="pips"`, prices = `entry_mid ± pips*0.10`.
  - Else `tp_mode="price"`; **sanity clamp: drop any target with
    `|tp − entry_mid| > 0.10 × entry_mid`** (kills fat-finger typos like `19333`
    for 1933 — without this clamp one typo produces an R of −1589 and destroys
    everything).
  - `TP: open` → no number → no TP → signal dropped later.
- Add-on flag: `\b(again|agan|more)\b`.

### 3.2 GSN (`parse_goldscalperninja.py`)
- Candidate iff `(xauusd|gold)\s+(buy|sell)\s+price…` entry regex matches AND `\bsl\b` present.
- Entry: `(?:xauusd|gold)\s+(buy|sell)\s+(\d{3,5}(\.\d+)?)(\s*[-/]\s*(\d{3,5}(\.\d+)?))?`.
- SL: `\bsl\b\s*[:\-]?\s*(\d{3,5}(\.\d+)?)`.
- TP: same `finditer` approach with separators `[,/]`; keep only numbers `> 1000`
  (this channel posts absolute prices, typically 3 TPs); same 10% sanity clamp.
- Add-on flag: `\b(again|re-?entry|add|round\s*[2-9])\b`.

3-year files are built by running each parser over the `*_max` raw file, keeping
signals dated ≥ `2023-06-29`, requiring `entry_low` truthy and (`sl` non-null or
`tp_mode` set), stamping `date/msg_id/channel`.

---

## 4. Trade simulation semantics (`engine.py` — validated by `validate_engine.py`, 12/12)

For each signal (chronological), on the merged 1-min candle list:

1. **Entry index**: `bisect_right(candle_times, signal_time)`. If no candle within
   **3 days** after the signal → skip (`no_price`).
2. **Fill** = open of that candle (`market_next`). No slippage in base config.
3. **Eligibility filters** (in `simulate`): `sl` non-null; `|fill − sl| ≤ $40`
   (`max_sl_dollars`, parse-junk guard); `|fill − sl| / 0.10 ≥ 20`
   (`min_sl_pips`); TP parseable else skip.
4. **Targets**: `tp_mode="pips"` → `tp1 = fill ± min(tp_raw)*0.10`;
   `tp_mode="price"` → sort targets nearest-first from the fill (descending for
   sells) → `tp1` = nearest. **Strategy A uses tp1 only, full size.**
5. **Candle walk** from the entry candle (inclusive): for each candle,
   `stop_hit = low ≤ SL` (buy) / `high ≥ SL` (sell); `tgt_hit = high ≥ TP` (buy) /
   `low ≤ TP` (sell). **Same-candle tie → pessimistic: SL wins.** Gaps through a
   level fill AT the level (not the gap price).
6. **P&L**: realized price move − 3 pips × $0.10 (round-trip cost). `R = realized /
   |fill − sl|`. Record `exit_date` (candle timestamp of the exit; end-of-data
   closes at last close).
7. Per-trade features recorded for filtering (all computed from data BEFORE the
   entry index — no lookahead): entry hour/weekday (UTC), `sl_pips`, addon flag,
   `ret4h/ret24h` (fill-open minus close 240/1440 candles back), `atr60` (mean
   high−low of prior 60 candles).

**Filter F1 (locked)** applied to the trade stream: `side == sell`, `addon == False`,
`40 ≤ sl_pips ≤ 120`. Then **FCFS**: walk trades sorted by entry epoch; drop any
whose entry epoch < the running `busy_until`; else take it and set
`busy_until = exit_epoch`.

---

## 5. Evaluation semantics (the prop simulator — `research/dynamic_risk.py` is authoritative for ladder numbers)

- **Trading day**: `day = floor((epoch + 3·3600) / 86400)` — i.e. days cut at
  21:00 UTC. Day list = only days with ≥ 1 (post-FCFS, post-filter) trade; "days to
  pass" counts these days, inclusive. (Calendar time ≈ ×1.4.)
- **Rolling evaluation**: an independent fresh evaluation is started at EVERY day
  index of the window; each runs forward within the window only.
- **Per day, per trade** (chronological): if the intraday consecutive-loss count
  ≥ 2 → skip remaining trades that day. Else `pnl% = R × rung_risk`;
  update running `day_pnl`.
  - Breach checks after every trade: `day_pnl ≤ −4` → **breach**;
    `phase_equity + day_pnl ≤ −10` → **breach**.
  - Ladder update after every trade: win → rung 0; loss → `min(rung+1, 2)`.
  - Target check after every trade: `phase_equity + day_pnl ≥ target` →
    Phase 1 done (record days, reset equity to 0, target 6.0, SAME day continues)
    or Phase 2 done → **pass**.
- Phase equity is additive % of the phase-start balance (no compounding), matching
  standard prop accounting; each phase has its own fresh −10% static floor.
- Starts that hit neither pass nor breach before the window ends are
  **incomplete** and excluded from the pass-rate denominator
  (`pass_rate = passes / (passes + breaches)`).

---

## 6. Why these specific choices (evidence, brief)

- **Sell-only**: over the study year buys lost −34.6R (0% pass); sells are pullback
  scalps that stayed profitable (+11.7R OOS) even in the rising 2026 market.
- **Strategy A** beat B/C/D-ladders under FCFS+stress: simplest exit, highest OOS
  sample (46+ resolved starts), 96–100% stressed pass. (D-family variants had
  too-few resolved OOS starts or collapsed under stress.)
- **SL 40–120p**: >120p stops carry the losing tail (`gt80-only` = −8.1R); <40p
  are sizing/compliance problems.
- **Risk ladder** beats every fixed risk and every step-down scheme tested: with a
  67% win rate most trades fire at the top rung, while losing streaks automatically
  de-size. Fixed 2% risk = 0% pass (two losses = −4% daily breach); ladder
  1.75/1.25/1.0 keeps worst day at −3.2%.
- **Consec-2 daily stop**: chosen over %-stops (risk-agnostic; the Jan-27-style
  6-loss day is capped at 2 losses) and over consec-3 (which breaches −4% at
  higher risks).

---

## 7. Reproduction procedure (agent instructions)

> **NOTE (repo cleanup 2026-07-26):** the `research/` pipeline and legacy analysis
> scripts were removed from the main branch to keep the repo lean. To reproduce
> the numbers, first check out the tagged snapshot that contains everything:
> `git checkout research-v1` — then follow the steps below exactly as written
> (paths in this section refer to that tag's layout, where `engine.py` and the
> parsers live at repo root). On the current main branch the same modules live in
> `src/` (`parse_signals.py` → `src/parse_gary.py`,
> `parse_goldscalperninja.py` → `src/parse_gsn.py`).

Environment: Linux/WSL, Python 3 with `numpy`, `pyyaml` (`pip install -r
requirements.txt` covers the rest). Working dir = repo root. The preserved `data/`
files from §2 must be in place. `parameters.yml` must have `filters.min_sl_pips: 20`,
`filters.max_sl_dollars: 40`, `market.pip_value: 0.10`.

```bash
# 0. sanity: engine semantics
python3 validate_engine.py                      # MUST print: 12 passed, 0 failed

# 1. candle cache (npz; auto-built on first use)
python3 -c "import sys; sys.path.insert(0,'research'); import sweep_lib as SL; SL.build_candle_cache()"

# 2. per-(channel,variant) trade streams, last-1-year window
for ch in gary gsn; do for v in A B C C_f07 D3 D5 D5_noBE D8 D5_hold480 zone_D5 zone_C; do
  python3 research/precompute_trades.py $ch $v 3; done; done
# stress streams for the A variant:
python3 research/precompute_trades.py gary A 4 0.5
python3 research/precompute_trades.py gsn  A 4 0.5

# 3. (optional, re-derives the filter choice) screen + deep eval
python3 research/screen.py                      # 85,536 combos -> candidates.json
for i in 0 1 2 3 4 5; do python3 research/deep_eval.py --chunk $i --nchunks 6; done

# 4. verification lenses (FCFS / stress / 3y regime / neighbors)
python3 research/verify.py fcfs
python3 research/verify.py stress
python3 research/verify.py regime3y
python3 research/verify.py neighbors

# 5. days detail + THE LOCKED LADDER NUMBERS
python3 research/days_detail.py
python3 research/dynamic_risk.py                # <- authoritative final table
```

Determinism: everything is pure deterministic computation over the preserved files —
no randomness, no network. Any numeric drift means an input file or a filter
constant differs.

---

## 8. Repro checkpoints (intermediate — verify before trusting the final table)

| Checkpoint | Expected |
|---|---|
| `validate_engine.py` | `12 passed, 0 failed` |
| candle cache | 1,114,084 bars |
| `precompute gary A 3` | `n=590 win=56.1% R=-69.9` |
| `precompute gary D5 3` | `n=590 win=56.4% R=-27.5` |
| `precompute gsn A 3` | `n=480 win=60.2% R=-24.2` |
| `precompute gsn D5 3` | `n=480 win=61.0% R=-6.2` |
| `screen.py` | `85536 combos -> 2065 survivors, kept 400` |
| F1 stream after filters+FCFS (in `verify.py fcfs`) | `n=297, R=+23.6, testR=+11.7`, 34 dropped by FCFS |
| 3y regime check (merged A, F1 filters) | 2023 +8.0R / 2024 **−86.5R** / 2025 −35.3R / 2026 +11.5R |

---

## 9. FINAL NUMBERS (from `research/dynamic_risk.py` — the locked table)

F1 stream (merged A, sells, fresh, SL 40–120p, FCFS), consec-2 daily stop.
TEST = OOS Mar–Jun 2026; FULL = Jul 2025–Jun 2026. `days` are trading days for BOTH
phases; pass% = passes/(passes+breaches) over rolling starts.

3-pip cost:

| Scheme | TEST | TEST days min/p25/med/p75/max | FULL | worst day |
|---|---|---|---|---|
| **LADDER 1.75/1.25/1.0 (LOCKED)** | **54/54 (100%)** | 12/23/**30**/38/57 | **143/146 (98%)** | **−3.1%** |
| Ladder 2.0/1.5/1.0 (aggressive alt) | 56/56 (100%) | 11/20/25/32/51 | 135/148 (91%) | −3.7% |
| Ladder 1.5/1.25/1.0 | 45/45 (100%) | 19/30/34/43/58 | 137/137 (100%) | −2.8% |
| Fixed 1.25% (baseline) | 36/36 (100%) | 31/36/43/50/62 | 128/128 (100%) | −2.6% |
| Fixed 1.5% (baseline) | 46/46 (100%) | 19/29/34/42/58 | 138/138 (100%) | −3.1% |

Stress (4-pip cost + 0.5-pip slippage):

| Scheme | TEST | FULL | worst day |
|---|---|---|---|
| **LADDER 1.75/1.25/1.0 (LOCKED)** | **48/48 (100%)**, med 34 | **118/140 (84%)** | −3.1% |
| Ladder 2.0/1.5/1.0 | 53/53 (100%), med 29 | 100/145 (69%) | −3.7% |
| Fixed 1.25% | 32/32 (100%), med 46 | 106/110 (96%) | −2.6% |

Full per-start tables (every evaluation start date with P1/P2/total days) print from
`research/days_detail.py` (fixed risks) and `research/dynamic_risk.py` (ladders).
Failure clustering: all full-year breaches sit in the Oct-9–28 2025 chop and
Jan-12–26 2026; fastest passes are Aug–Sep 2025 starts (4–12 days).

---

## 10. Confidence & the regime caveat (mandatory context — do not drop)

- The OOS "100%" is one 4-month window with heavily overlapping starts (~2–3
  statistically independent samples, not 56). Calibrated forward estimate for a
  live account passing, if the regime holds: **~65–75%**.
- **The same filters lost −86.5R in 2024 and −35.3R in 2025** (3-year check). The
  sell-scalp edge switched on ~mid-2025. It will decay without notice — hence the
  kill-switch in §1 and a paper-trading gate before any live deployment.
- Costs assumed 3p round-trip = verified realistic for GFT gold (raw spread
  ~$0.15–0.25 + $5/lot/side commission); the 84% stressed number is the
  planning-pessimistic case.

---

## 11. Deployment architecture (target: 24/7 listener + MT5 execution)

Single Windows host (forex VPS recommended for live; home PC acceptable for paper).
`MetaTrader5` Python package requires Windows + a running, logged-in MT5 terminal.

Components (each its own process, state shared via SQLite):

1. **`listener.py`** — Telethon `events.NewMessage` on both channels; parse with §3
   parsers; act only on first post (ignore edits/deletes); enqueue
   `{channel, msg_id, ts, side, sl, tps, raw}`.
2. **`executor.py`** — the only component that trades:
   - Gates in order: kill-switch → market open → no open position (FCFS) →
     signal age ≤ 3 min → sell-only → fresh-only → daily consec-2 stop →
     compute fill-side `sl_pips` from live price, require 40–120 → spread ≤ cap
     (~5 pips) → size = `rung% × account / (sl_pips × $10/lot)` snapped down to
     0.01 (min 0.01).
   - `order_send` market sell with SL + TP1 attached; store ticket.
   - Poll position/history; on close compute realized R → update ladder rung,
     intraday streak, rolling-60 R, monthly P&L → journal every decision
     (take/skip + reason).
3. **`watchdog.py`** — process + MT5-connection health, restarts, heartbeat and
   trade/kill-switch alerts to a personal Telegram bot.

Crash-safety invariants: all risk state (rung, streak, day key, kill-switch
tallies) persisted before order submission; on startup reconcile SQLite vs MT5 open
positions + trade history before accepting signals.

Rollout: **(1)** paper mode on an MT5 demo 4–6 weeks — weekly comparison of win%,
avg R, fill quality vs §9 expectations; **(2)** live on ONE GFT evaluation only if
paper tracks backtest; **(3)** same EA unchanged into the funded phase. One account
only, ever (GFT ruling condition).

---

## 11b. Live pipeline (built 2026-08-14)

Message → decision, implemented in `src/pipeline.py`:

1. **Classify** (`gemini_classifier.py`, `gemini-3.1-flash-lite`, temp 0, strict JSON)
   → `entry` | `update` | `noise`. Never places a trade itself.
2. **Cross-check** every entry against the regex parser. Side or SL disagreement → SKIP.
3. **Guardrails** (`llm_parser.validate_extraction`, shared by both LLM providers):
   no invented numbers, SL/TP geometry, SL band, 10% TP sanity, live-price drift.
4. **Strategy gates**: sell-only, fresh-only, SL 40–120p, one-trade-at-a-time.
5. **Size**: `lots = risk% x equity / (SL_pips x $10)`, floored to the 0.01 step.
6. **Register** PENDING in `trade_registry.py` (SQLite), then the executor places the
   order with SL+TP **attached to the order itself** — a position can never run naked.

**Correlation of updates** (`TradeRegistry.correlate`): edit of a known `msg_id` →
`reply_to_msg_id` → the sole open trade → else alert. Never guesses.

**Two gates default to OFF so launch matches the backtest exactly:**

| Flag | Default | Why |
|---|---|---|
| `allow_default_sl_entry` | `false` | Entering a signal with no stated SL is an untested trade class (~13 posts/yr). The 90p default still rides on every order as the safety net. |
| `follow_provider_updates` | `false` | Acting on "move SL to BE" changes the exit distribution the backtest measured (Strategy A = close at TP1) and weakens the GFT "own logic" position. Updates are classified and journalled, never acted on. |

Defaults `sl_pips: 90` / `tp_pips: 50` are the medians of the 271-trade locked stream.
Note the stop distance does **not** control drawdown — position sizing does; any SL
distance yields the same % loss. The default is chosen to keep lot sizes usable.

## 12. File inventory

**Current main branch (lean layout):**

| File | Role |
|---|---|
| `src/engine.py` | candle-walk trade simulator (all exit strategies, `exit_date`) |
| `src/parse_gary.py` / `src/parse_gsn.py` | per-channel parsers (§3) |
| `src/fetch_channel.py` | fetch any channel's history (`python3 src/fetch_channel.py <chat> <tag> [months]`) |
| `src/fetch_prices.py` | TwelveData 1-min XAU/USD day fetcher (cached) |
| `src/login.py` / `src/list_chats.py` | Telegram auth + chat discovery helpers |
| `tests/validate_engine.py` | 12 synthetic engine tests — must stay green |
| `data_archive/` | frozen research inputs (§2) |
| `docs/goat_support_question.md` | GFT compliance question (answered/approved) |

**Research pipeline (git tag `research-v1` only):**

| File | Role |
|---|---|
| `engine.py` | candle-walk trade simulator (all exit strategies; returns R + `exit_date`) |
| `validate_engine.py` | 12 synthetic-candle correctness tests — run before trusting anything |
| `parse_signals.py` / `parse_goldscalperninja.py` | per-channel parsers (§3) |
| `research/sweep_lib.py` | windows/constants, candle cache, features, masks, day aggregation, two-phase evaluator |
| `research/precompute_trades.py` | one (channel, variant, cost, slip) → trade-stream JSONL |
| `research/screen.py` | 85k-combo train-window screen → `candidates.json` |
| `research/deep_eval.py` | rolling two-phase train/test/full × risk × stop for candidates |
| `research/verify.py` | 4 adversarial lenses: `fcfs` / `stress` / `regime3y` / `neighbors` |
| `research/days_detail.py` | fixed-risk speed levers + full per-start days tables |
| `research/dynamic_risk.py` | **ladder evaluator — authoritative for §9** |

Version note: `engine.py` gained the `exit_date` field on 2026-06-30 (needed for
FCFS); `validate_engine.py` still passes 12/12 — any future engine change must keep
it green and re-verify §8 checkpoints.
