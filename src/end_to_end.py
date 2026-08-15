"""
END-TO-END run: real Telegram messages -> Gemini -> pipeline -> risk state ->
broker -> registry, with positions actually opening and closing on real prices.

    python3 src/end_to_end.py            # replay cassette classifications (FREE)
    python3 src/end_to_end.py --live 25  # classify 25 fresh messages via Gemini

Uses PaperBroker against the real 1-min XAUUSD feed, so trades fill, hit SL/TP,
and produce realized R — which drives the risk ladder, the daily stop and the
kill switch exactly as they will in production. The only untested leg is the
MT5Broker itself, which needs Windows.
"""
import csv
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml
import parse_gary, parse_gsn
from trade_registry import TradeRegistry
from risk_state import RiskState
from pipeline import handle_message
from broker import PaperBroker
from executor import Executor

CASSETTE = ROOT / "tests" / "gemini_corpus_cassette.json"


class Replay:
    def __init__(self, d):
        self.__dict__.update(d)
        self.side = None if self.side == "none" else self.side
        self.tp_unit = None if self.tp_unit == "none" else self.tp_unit
        self.sl = self.sl or None
        self.entry_low = self.entry_low or None
        self.entry_high = self.entry_high or None
        self.tps = list(self.tps or [])


def load_candles(lo, hi):
    out = []
    for name in ("xauusd_1m_3y.csv", "xauusd_1m_recent.csv"):
        p = ROOT / "data" / name
        if not p.exists():
            continue
        with p.open() as f:
            for r in csv.DictReader(f):
                dt = datetime.fromisoformat(r["datetime_utc"]).replace(tzinfo=timezone.utc)
                if lo <= dt <= hi:
                    out.append((dt, float(r["open"]), float(r["high"]),
                                float(r["low"]), float(r["close"])))
    out.sort(key=lambda c: c[0])
    return out


def main():
    cfg = yaml.safe_load((ROOT / "parameters.yml").read_text())
    live = cfg["live"]
    rows = json.loads(CASSETTE.read_text())
    rows.sort(key=lambda r: r["date"])

    lo = datetime.fromisoformat(rows[0]["date"]) - timedelta(minutes=5)
    hi = datetime.fromisoformat(rows[-1]["date"]) + timedelta(days=10)
    candles = load_candles(lo, hi)

    reg = TradeRegistry(":memory:")
    risk = RiskState(reg, cfg)
    broker = PaperBroker(candles, spread_pips=cfg["market"]["round_trip_cost_pips"])
    ex = Executor(broker, reg, cfg)

    print("=" * 92)
    print("END-TO-END  telegram -> gemini -> pipeline -> risk -> broker -> registry")
    print(f"  {len(rows)} messages | {len(candles):,} candles | profile={live['account_profile']}"
          f" | ladder={live['risk_profiles'][live['account_profile']]['risk_ladder_pct']}")
    print("=" * 92)

    ex.reconcile()          # startup truth check (no-op on a fresh DB)
    tally, closes = {}, []

    for r in rows:
        when = datetime.fromisoformat(r["date"])
        closes += ex.advance_to(when)     # closes SL/TP hits en route
        if broker.now() is None:
            break

        sig = Replay(r["gemini"])
        P = parse_gary if r["channel"] == "gary" else parse_gsn
        rx = P.parse(r["text"]) if P.looks_like_signal(r["text"]) else None
        bid, ask = broker.price()

        d = handle_message(
            {"msg_id": r["msg_id"], "channel": r["channel"], "date": r["date"],
             "text": r["text"]},
            reg, cfg, lambda _t, s=sig: s,
            live_price=(bid + ask) / 2, regex_sig=rx, risk=risk)

        res = ex.on_decision(d)
        tally[d["decision"]] = tally.get(d["decision"], 0) + 1

        if d["decision"] == "take":
            snap = risk.snapshot(r["date"])
            print(f" ▶ {r['date'][:16]} {r['channel']:4} TAKE {d['side']} "
                  f"{d['lots']:.2f}lots @ {res.get('price')} SL {d['sl']} TP {d['tp']} "
                  f"| rung {d['rung_index']} ({d['risk_pct']}%) ticket {res.get('ticket')}")
        elif d.get("risk_block"):
            print(f" ⛔ {r['date'][:16]} {r['channel']:4} BLOCKED — {d['reason']}")

    # drain the remaining market so open trades resolve
    while broker.i < len(broker.candles) and broker.open:
        closes += [ex._record(c) for c in broker.step()]

    print("-" * 92)
    for c in closes:
        print(f" ■ closed msg {c['msg_id']} by {c['reason'].upper():2} -> R {c['r']:+.2f}")

    print("=" * 92)
    print("decisions:", ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    s = risk.snapshot()
    print(f"risk state: closed={s['closed_trades']} rung={s['rung_index']} "
          f"({s['risk_pct']}%) dayLosses={s['day_consec_losses']} "
          f"rollingR={s['rolling_r']} killed={s['killed']}")
    print(f"registry:   {len(reg.open_trades())} still open | {len(reg.events())} journal rows")
    tot = sum(c["r"] for c in closes)
    if closes:
        wins = sum(1 for c in closes if c["r"] > 0)
        print(f"result:     {len(closes)} trades, {wins} wins, net {tot:+.2f}R")


if __name__ == "__main__":
    main()
