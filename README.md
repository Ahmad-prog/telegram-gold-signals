# telegram-gold-signals

Research-validated system for trading XAUUSD (gold) signals from two public
Telegram channels — **GARY GOLD TRADER** and **GoldScalperNinja** — with a locked,
backtested strategy targeting GoatFundedTrader prop-firm evaluations.

> ⚠️ Research/educational project. Automating a personal Telegram account is
> against Telegram's ToS; trading involves substantial risk of loss. The edge is
> **regime-fitted** (see `docs/design-doc.md` §10) — a kill-switch is part of the design.

---

## The locked strategy (full spec: [`docs/design-doc.md`](docs/design-doc.md))

| Rule | Value |
|---|---|
| Signals | both channels, merged — **SELL only**, fresh only (no add-ons), SL 40–120 pips |
| Position | **one trade at a time** (first-come-first-served), market entry |
| Exit | Strategy A — 100% close at the nearest TP, SL at signal stop |
| Risk | ladder **1.75% → 1.25% → 1.0%** (down one rung per loss, reset on any win) |
| Daily stop | 2 consecutive losing trades → done for the day (resets 21:00 UTC) |
| Kill-switch | rolling 60-trade R ≤ −10R, or 2 losing months → stop |

**Backtest (last-1-year, out-of-sample validated, cost-stressed): 100% of rolling
evaluation starts pass GFT Phase 1+2, median ~30 trading days.** Machine-readable
config: `parameters.yml → live:`.

## Repository layout

```
parameters.yml         all knobs incl. the locked `live:` strategy block
src/
  engine.py            1-min candle-walk backtest engine (exit strategies A/B/C/D)
  parse_gary.py        GARY GOLD TRADER message parser
  parse_gsn.py         GoldScalperNinja message parser
  fetch_channel.py     pull any channel's history -> data/raw_<tag>.jsonl
  fetch_prices.py      TwelveData 1-min XAU/USD fetcher (per-day cache)
  login.py             one-time Telegram login (file-driven, no prompt)
  smoke_test.py        end-to-end test: telegram -> parse -> gates -> alert
  list_chats.py        helper: find chat/sender handles
  gemini_classifier.py Gemini front door: entry / update / noise + extraction
  llm_parser.py        Claude second-opinion parser + SHARED guardrails
  trade_registry.py    SQLite registry: msg_id -> ticket, state machine
  pipeline.py          orchestrator: classify -> cross-check -> gates -> registry
  paper_run.py         end-to-end paper run over recorded messages (no broker)
  audit_channel.py     score any candidate channel against the hiring bar
tests/
  validate_engine.py       12 synthetic-candle engine tests
  test_pipeline.py         30 registry/pipeline tests (no API)
  test_llm_guardrails.py   20 guardrail tests (no API)
  test_gemini_cases.py     15 classifier cases (cassette replay, free)
  test_gemini_corpus.py    26 real messages vs regex (cassette replay, free)
data_archive/          frozen research inputs (gzipped) — restores data/, see its README
docs/
  design-doc.md        ★ normative spec: strategy, exact semantics, repro checkpoints
  goat_support_question.md   GFT compliance question (answered & approved in writing)
```

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env            # fill Telegram + TwelveData keys (see design-doc)

# restore the frozen dataset
mkdir -p data
for f in data_archive/*.gz; do gzip -dkc "$f" > "data/$(basename "${f%.gz}")"; done

# verify the engine
python3 tests/validate_engine.py     # -> 12 passed, 0 failed
```

## Reproducing the research numbers

The full research pipeline (config sweep, OOS validation, verification lenses) is
preserved at git tag **`research-v1`**:

```bash
git checkout research-v1
# then follow docs/design-doc.md §7 step by step; §8 lists the expected checkpoints
```

## Status / roadmap

- [x] Data pipeline, engine, parsers, 3-year + 1-year studies
- [x] Strategy locked (`design-doc.md`, 2026-06-30) + GFT compliance confirmed in writing
- [x] Gemini classifier + trade registry + pipeline, **77 tests green**, paper run end-to-end
- [ ] **Next:** `listener.py` (live Telegram feed) + `executor.py` (MT5 order_send) + `watchdog.py`
- [ ] 4–6 weeks paper validation on MT5 demo → one GFT evaluation
