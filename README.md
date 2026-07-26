# GARY GOLD TRADER — Signal Backtesting System

Fetches gold (XAUUSD) buy/sell signals from the **GARY GOLD TRADER** Telegram
channel, parses them into structured data, fetches matching 1-minute price
history, and **rigorously backtests + stress-tests** how profitable it is to
follow them — under realistic prop-firm trading costs.

> ⚠️ Research/education tool. Automating a personal Telegram account is against
> Telegram's ToS; auto-trading third-party signals is high risk. Validate on
> history (this repo) before risking real or funded capital.

---

## Results summary (6 months: Dec 2025 → Jun 2026, 397 signals)

**★ LOCKED config: Strategy D (split + breakeven + 5-rung trailing ladder),
market entry, 3-pip cost, 0.5% risk/trade.**

| Metric | Value |
|---|---|
| Trades | 331 (66 skipped: no TP / parse junk) |
| Win rate | **64.4%** |
| Net result | **+40.9 R  /  +20.4% of account** |
| Profit factor | **1.34** |
| Max drawdown | −13 R (−6.5% of account) |

**Prop-firm result (GOAT rules: 1% daily-stop logic, static −10% max, −4% daily
reset 21:00 UTC, +8%/+6% targets) at 0.5% risk:**
- **Phase 1 (+8%) PASS, Phase 2 (+6%) PASS** — worst day −2.07%, max DD −2.69%.
- **Rolling-start robustness: 100% of start dates pass** at 0.5% risk (vs only
  ~62% for D at 1% risk, which breaches the 10% floor if you start before a dip).
- **~7–8 weeks to funded** from a representative start.

**Key findings from stress testing (288 combinations) + prop sim:**
1. **Enter at MARKET, not on a zone pullback.** Waiting for price to retrace
   into his entry zone (`zone_touch`) *loses* money (−17.6R, PF 0.87) — you miss
   the runners and only catch the losers. He says "Buy **NOW**" for a reason.
2. **Robust to costs.** With market entry, every exit strategy stays profitable
   even at a brutal 5-pip cost + 1-pip slippage.
3. **Risk SIZE is the real safety control, not the exit.** At 1% risk only
   Strategy A reliably passes the prop drawdown (96%); C/D breach ~35% of start
   dates. At **0.5% risk both A and D pass 100%** — and D funds faster.
4. **Strategy depends on goal:** max raw return → D (+40.9R); reliably pass a
   10%-static-DD challenge → 0.5% risk + D (or A for more regime-robustness, but
   A is too slow to hit targets at 0.5%).
5. **Your 2% daily-stop works** — capped the worst day at −2.07% in every test.
6. **Buys > sells** (+22R vs +5R). Apr/May were negative months — expect losing
   stretches; D's edge depends on gold trending.

---

---

## Multi-channel — 2nd provider added: GoldScalperNinja (2026-06-29)

A second XAUUSD signal channel, **GoldScalperNinja** (`@GoldScalperNinja`), was
added alongside Gary and put through the *same* validation. Its format differs
(`💰 XAUUSD BUY 4039 - 4033 / 🚨 SL : 4028 / 💵 TP : 4044, 4049, 4059` — 3
absolute-price TPs), so it gets its own parser.

**GSN standalone (Strategy D, market entry, 3p, 0.5% risk):** 248 trades,
**74.2% win**, +10.4R, PF 1.20, max DD −6.0R. Real edge + robust to cost, but
edge-per-trade is thin → too slow to pass +8% alone at 0.5%. Best used as a
*diversifier* (occupies the account only ~3% of the time).

**★ Merged Gary + GSN on ONE account, one-trade-at-a-time, first-come-first-served:**

| Metric (0.5% risk) | Gary alone | **Gary + GSN merged** |
|---|---|---|
| Trades | 259 | **416** |
| Win rate | 64.5% | **67.5%** |
| Net result | +20.1R (+10.1%) | **+30.3R (+15.1%)** |
| Max drawdown | −9.8R | **−8.9R** (better) |
| Days to +8% | slower | **median 32d** |
| Rolling-start prop pass | 100% | **100% (P1 44/44, P2 55/55)** |

Adding GSN is **nearly additive** (Gary +20.6R, GSN +9.7R) — the two barely
interfere because combined occupancy is only 8.3%. Net: ~50% faster funding,
slightly *safer* drawdown.

### Account modes (parameters.yml → `account`, run via account_sim.py)
The bot is parameterized for three account types:
- `evaluation` — pass the +8%/+6% challenge without breaching firm limits.
- `funded` — trade firm capital; a breach LOSES the account (no pass target).
- `real` — your own money; no firm limits, track DRAWDOWN + risk of RUIN.
- `compounding: true/false` — risk % of current equity vs starting balance.
- `daily_stop_mode` — **`consec_losses` (default, =2)** stops the day after 2 losses
  **in a row** (a win resets the streak); also `net_losses` (stop when losses−wins ≥ N)
  and `pct` (fixed % loss). The consec rule gives the smallest worst-day at funded
  risk (0.5–1%): worst day −1.3% @0.5%, −2.6% @1%. Note it only bounds *back-to-back*
  losses — a win between losses resets the streak, so add `pct` as a hard floor only
  if running an aggressive (5%) real account.

**⚠️ Real account at 5%/trade is a boom-or-bust gamble:** +67.6% return BUT
**−36.3% max drawdown** (the −8.9R drawdown × 10). A 6-loss streak already
occurred in the data (≈ −30% at 5%); a 9–10 streak ≈ ruin. Keep funded risk at
**0.5–1%**; for an aggressive *real* account, 2–3% is the sane ceiling.

New scripts for this work:
| File | What it does |
|---|---|
| `fetch_channel.py <chat> <tag> [months]` | Generalized fetch for ANY channel (tags `source`) |
| `parse_goldscalperninja.py` | Parser for GSN's format → `signals_goldscalperninja.jsonl` |
| `validate_channel.py <signals_file>` | Runs Gary's full validation on any channel |
| `merged_sim.py` | Gary + GSN on one account, 1-at-a-time, FCFS |
| `account_sim.py [mode] [risk%]` | Runs merged strategy as evaluation / funded / real |

---

## Folder structure

```
telegram_signals/
├── parameters.yml          # ★ central config — all tunable knobs live here
├── .env                    # secrets (API keys, phone) — gitignored
├── requirements.txt
│
├── login.py                # one-time Telegram login (file-driven, no prompt)
├── fetch_history.py        # STEP 1: pull 6mo of channel messages -> raw_messages.jsonl
├── parse_signals.py        # STEP 2: parse raw text -> structured signals.jsonl
├── fetch_prices.py         # STEP 3a: pull 1-min XAUUSD price history -> xauusd_1m.csv
│
├── engine.py               # core backtest engine (config-driven, importable)
├── backtest.py             # STEP 3b: run one backtest per parameters.yml
├── stress_test.py          # STEP 3c: sweep all param combos, find best/robust
├── prop_sim.py             # STEP 3d: prop-firm pass/fail sim + rolling-start test
├── analyze.py              # STEP 3e: loss/accuracy deep-dive + improvement tests
├── compliant_sim.py        # STEP 3f: GOAT-compliant portfolio sim (no-hedge etc.)
├── validate_engine.py      # rigid synthetic tests proving the engine is correct
│
├── list_chats.py           # helper: find a chat / sender handle
├── diag_code.py            # helper: diagnose Telegram login-code delivery
│
└── data/
    ├── raw_messages.jsonl   # step 1 output (all channel text)
    ├── signals.jsonl        # step 2 output (397 structured signals)
    ├── unparsed.jsonl       # signal-looking msgs we couldn't parse (audit)
    ├── xauusd_1m.csv         # step 3a output (~220k 1-min bars)
    ├── prices_raw/           # per-day price cache (re-run safe)
    ├── backtest_trades.csv   # per-trade log from backtest.py
    └── stress_results.csv    # full sweep matrix from stress_test.py
```

---

## Architecture / data flow

```
 Telegram channel
       │  login.py (once)  →  signals.session (cached auth)
       ▼
 fetch_history.py ──► data/raw_messages.jsonl     (raw text, 6 months)
       ▼
 parse_signals.py ──► data/signals.jsonl          (side, entry zone, SL, TPs)
       │
 TwelveData API
       ▼
 fetch_prices.py  ──► data/xauusd_1m.csv          (1-min OHLC for signal days)
       │
       ├──────────────┐
       ▼              ▼
 backtest.py     stress_test.py     ← both import engine.py, read parameters.yml
   (one run)      (216-combo sweep)
       ▼              ▼
 backtest_trades  stress_results.csv

 validate_engine.py  ← proves engine.py is correct on synthetic candles
```

The **engine** simulates each signal candle-by-candle: entry → manage (exit
strategy) → SL/TP, applying costs and slippage. Same-candle SL+TP collisions are
resolved pessimistically (assume SL first) by default.

---

## Quick setup

```bash
cd telegram_signals
pip install -r requirements.txt
cp .env.example .env          # then fill in the values below
```

`.env` needs:
| Key | What | Where |
|---|---|---|
| `TG_API_ID`, `TG_API_HASH` | Telegram app creds | https://my.telegram.org/apps |
| `TG_PHONE` | your number, e.g. `+49...` | — |
| `TG_PASSWORD` | only if 2FA enabled | — |
| `TG_CHAT` | `https://t.me/Gary_TheTrader` | — |
| `TWELVEDATA_KEY` | free price-data key | https://twelvedata.com/register |

> In Pakistan/blocked regions: Telegram needs a VPN. If the script can't connect,
> it's because WSL/Linux isn't routing through the system VPN — use a proxy or
> Cloudflare WARP. (my.telegram.org's "Create app" also rejects datacenter/VPN
> IPs — use a residential IP or WARP.)

---

## How to run (in order)

```bash
# 1. Log in to Telegram (one time — caches a session)
python login.py                 # then write the code Telegram sends:
                                #   echo 12345 > login_code.txt

# 2. Pull 6 months of channel history
python fetch_history.py         # -> data/raw_messages.jsonl

# 3. Parse into structured signals
python parse_signals.py         # -> data/signals.jsonl

# 4. Fetch 1-min gold prices (~20 min, free-tier rate limits; cached)
python fetch_prices.py          # -> data/xauusd_1m.csv

# 5. Verify the engine is correct (should print "10 passed, 0 failed")
python validate_engine.py

# 6. Run the backtest (uses parameters.yml)
python backtest.py              # -> data/backtest_trades.csv + summary

# 7. Stress-test every parameter combination
python stress_test.py           # -> data/stress_results.csv + analysis

# 8. Prop-firm pass/fail + rolling-start robustness
python prop_sim.py              # PASS/FAIL per phase, days-to-fund, % of starts that pass

# 9. Loss/accuracy deep-dive + improvement tests (trend filter etc.)
python analyze.py

# 10. GOAT-compliant portfolio sim (no-hedge, no-add-to-loser, trend filter, min-SL)
python compliant_sim.py         # what the bot ACTUALLY does on one funded account
```

## ✅ GoatFundedTrader compliance — APPROVED IN WRITING (2026-06-29)

GFT support confirmed in writing that this approach **complies with their rules**.
This was the one risk that couldn't be solved in code; it is now cleared.

> GFT's ruling (paraphrased): *"Using public signal ideas as entry triggers is
> NOT a prohibited third-party strategy, as long as the EA logic is your own and
> you are not copying trades between accounts."* They also affirmed: fixed risk,
> no martingale/grid, no hedging, one trade at a time, single account = compliant.

**The ruling is CONDITIONAL on that exact design — treat it as frozen:**
- EA logic stays your own (your risk sizing + Strategy-D exit management).
- **One account only** (eval + funded, same EA); never copy trades between accounts.
- **Fixed risk 0.5–1%** on funded — NOT 5% (breaches the −10% limit anyway).
- **One trade at a time**, FCFS, never two positions open; no hedging.

Keep the support reply / ticket number as written proof for any future payout
dispute. (Question template that was sent: `goat_support_question.md`.)

## GoatFundedTrader compliance (baked into compliant_sim.py + the live bot)

The naive backtest lets positions overlap freely; a real single account can't.
`compliant_sim.py` enforces GOAT's rules and is the authoritative result:
- **No hedging** — never hold opposite positions (skip or flip the opposing signal).
- **No add-to-loser** — ignore "Buy More/Again" while the open position is underwater.
- **Min-stop filter** — skip ultra-tight stops (huge lots → "too risky"/large-volume).
- **Max concurrent** positions cap; **one account only** (no duplicating trades/ideas).
- (Trend filter exists as an option but is OFF — we take every Gary signal as-is;
  only the EXIT management is our own logic, so his entry selection is unchanged.)

Compliant result (Strategy D, 0.5% risk, no trend filter): **306 trades, 64.7%
win, +22.1R (+11.1% acct), PF 1.20, −6.9% max DD; passes both phases (worst day
−2.07%).** Cost of compliance vs naive ≈ −2.6R (drops 16 hedge + 8 add-to-loser
signals). ⚠️ Whether signal-following itself counts as a prohibited "third-party
strategy" is GOAT's call — confirm with their support before paying.

---

## parameters.yml reference

All behavior is controlled here — never hard-code in scripts.

| Section | Key | Meaning |
|---|---|---|
| `market` | `pip_value` | $ per pip for XAUUSD (0.10) |
| | `round_trip_cost_pips` | spread+commission per trade (default 3) |
| | `slippage_pips` | adverse fill per side |
| `entry` | `mode` | `market_next` (recommended) or `zone_touch` |
| `exit` | `strategy` | `A` (TP1), `B` (TP2), `C` (split+breakeven) |
| | `same_candle_tie` | `pessimistic` (SL first) or `optimistic` |
| | `no_time_exit` | `true` = run until SL/TP (no daily cutoff) |
| `risk` | `risk_per_trade_pct` | % of account risked per trade (1R) |
| `filters` | `max_sl_dollars` | skip parse-junk signals with huge SL |
| | `skip_no_tp` | skip signals without a parseable TP |
| `stress` | sweep ranges | values stress_test.py iterates over |

---

## Recommended live config (LOCKED — for a GOAT prop account)

```yaml
entry:  { mode: market_next }        # enter at market on the post (don't wait for zone)
exit:   { strategy: D, ladder_rungs: 5, move_to_breakeven: true }
market: { round_trip_cost_pips: 3 }  # set to YOUR broker's real gold cost
risk:   { risk_per_trade_pct: 0.5 }  # 0.5% -> 100% prop pass across all start dates
```
Keep the **2% daily self-stop** (2–4 stop-outs → stop for the day). Funds in
~7–8 weeks in backtest; expect slower in non-trending gold.

## Not built yet (next steps)
> Compliance is now cleared (GFT approved, see above), so the live side is the
> remaining work. Build it within the frozen design (1-at-a-time, FCFS, fixed risk).
- **Live listener** (`events.NewMessage` on BOTH channels → per-channel parser →
  emit the trade). Start in paper/alert mode: log the exact trade it *would* place.
- **Broker execution** (MT5 etc.) — enforce one-at-a-time / FCFS / fixed-risk /
  Strategy-D / no-hedge. Build & paper-test LAST and in isolation; that's the money risk.
