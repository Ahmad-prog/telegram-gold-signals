"""
SOAK TEST — the whole live stack over a year of real signals.

    python3 src/soak_test.py

Runs every real 2025-07..2026-06 signal through the LIVE code path
(pipeline -> risk state -> executor -> PaperBroker on real 1-min candles),
not the backtest engine. Purpose:

  1. exercise the risk machinery at volume — ladder steps, daily stops,
     kill-switch arming — which a 26-message run cannot do;
  2. cross-check the live path against the backtest. The two are different
     implementations of the same rules, so a large divergence means one of
     them is wrong.

Uses the regex parser as the classifier so it costs nothing to run.
"""
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml
from trade_registry import TradeRegistry
from risk_state import RiskState
from pipeline import handle_message
from broker import PaperBroker
from executor import Executor

START, END = "2025-07-01", "2026-06-29"


class RegexSig:
    """Adapts a regex-parsed signal to the classifier interface."""
    kind, action = "entry", "open"
    new_sl = new_tp = 0.0
    confidence, reason = 1.0, "regex"

    def __init__(self, s):
        self.side = s["side"]
        self.entry_low, self.entry_high = s["entry_low"], s["entry_high"]
        self.sl = s["sl"]
        self.tps = list(s["tp_raw"] or [])
        self.tp_unit = s["tp_mode"]
        self.is_addon = bool(s.get("addon"))


def load_signals():
    out = []
    for fn, ch in (("data/signals_gary_3y.jsonl", "gary"),
                   ("data/signals_gsn_3y.jsonl", "gsn")):
        for line in (ROOT / fn).open(encoding="utf-8"):
            s = json.loads(line)
            if not (START <= s["date"][:10] <= END):
                continue
            if s["sl"] is None or not s.get("tp_raw") or not s["tp_mode"]:
                continue
            out.append((ch, s))
    out.sort(key=lambda x: x[1]["date"])
    return out


def load_candles():
    rows = {}
    for name in ("xauusd_1m_3y.csv", "xauusd_1m_recent.csv"):
        p = ROOT / "data" / name
        if not p.exists():
            continue
        with p.open() as f:
            for r in csv.DictReader(f):
                d = r["datetime_utc"]
                if d[:10] < START:
                    continue
                rows[d] = (datetime.fromisoformat(d).replace(tzinfo=timezone.utc),
                           float(r["open"]), float(r["high"]),
                           float(r["low"]), float(r["close"]))
    return [rows[k] for k in sorted(rows)]


def main():
    cfg = yaml.safe_load((ROOT / "parameters.yml").read_text())
    live = cfg["live"]
    sigs = load_signals()
    candles = load_candles()

    reg = TradeRegistry(":memory:")
    risk = RiskState(reg, cfg)
    broker = PaperBroker(candles, spread_pips=cfg["market"]["round_trip_cost_pips"])
    ex = Executor(broker, reg, cfg)

    print("=" * 88)
    print("SOAK TEST — live code path over a year of real signals")
    print(f"  {len(sigs)} signals | {len(candles):,} candles | "
          f"ladder={live['risk_profiles'][live['account_profile']]['risk_ladder_pct']} "
          f"| daily stop={cfg['account']['daily_consec_loss_stop']} consec")
    print("=" * 88)

    tally = Counter()
    rung_hist = Counter()
    closes = []
    daily_blocks = kill_blocks = 0

    for ch, s in sigs:
        when = datetime.fromisoformat(s["date"])
        closes += ex.advance_to(when)     # closes SL/TP hits en route
        if broker.now() is None:
            break

        bid, ask = broker.price()
        d = handle_message(
            {"msg_id": s["msg_id"], "channel": ch, "date": s["date"], "text": s["raw"]},
            reg, cfg, lambda _t, sg=RegexSig(s): sg,
            live_price=(bid + ask) / 2, risk=risk)

        tally[d["decision"]] += 1
        if d.get("risk_block"):
            if "KILL" in d["reason"]:
                kill_blocks += 1
            else:
                daily_blocks += 1
        if d["decision"] == "take":
            rung_hist[d["rung_index"]] += 1
            ex.on_decision(d)

    while broker.i < len(broker.candles) and broker.open:
        closes += [ex._record(c) for c in broker.step()]

    print("\nDECISIONS")
    for k, v in tally.most_common():
        print(f"  {k:8} {v:5}")
    print(f"  (of the skips: {daily_blocks} daily-stop, {kill_blocks} kill-switch)")

    print("\nRISK LADDER — trades taken at each rung")
    rungs = live["risk_profiles"][live["account_profile"]]["risk_ladder_pct"]
    for i, pct in enumerate(rungs):
        print(f"  rung {i} ({pct:>4}%) : {rung_hist.get(i,0):4} trades")

    n = len(closes)
    if n:
        wins = sum(1 for c in closes if c["r"] > 0)
        tot = sum(c["r"] for c in closes)
        by = Counter(c["reason"] for c in closes)
        print(f"\nRESULT (live path)")
        print(f"  {n} trades | {wins} wins ({wins/n*100:.0f}%) | net {tot:+.1f}R "
              f"| avg {tot/n:+.3f}R")
        print(f"  exits: {dict(by)}")

    s = risk.snapshot()
    print(f"\nFINAL RISK STATE")
    print(f"  rung={s['rung_index']} ({s['risk_pct']}%)  rolling60R={s['rolling_r']}  "
          f"consecLosingMonths={s['consec_losing_months']}  killed={s['killed']}")
    if s["killed"]:
        print(f"  kill reason: {s['kill_reason']}")

    print("\nCROSS-CHECK vs backtest")
    print("  backtest (merged sells, fresh, 40-120p, FCFS, 3p): 271 trades, 68% win, +4.9R")
    print(f"  live path                                        : {n} trades, "
          f"{wins/n*100:.0f}% win, {tot:+.1f}R" if n else "  live path: no trades")
    print("  differences are expected: the live path also applies the daily stop and")
    print("  kill switch, which the trade-level backtest did not.")


if __name__ == "__main__":
    main()
