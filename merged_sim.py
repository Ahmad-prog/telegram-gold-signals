"""
MERGED single-account sim: Gary + GoldScalperNinja on ONE GOAT account,
ONE trade at a time, first-come-first-served.

  - merge both channels' signals, sort chronologically
  - if a position is already open, the incoming signal is DROPPED (never 2 at once)
  - manage each trade with Strategy D (locked config), 0.5% risk
  - report combined result, per-channel contribution, prop pass/fail,
    rolling-start robustness, and days-to-fund

    python merged_sim.py
"""

import bisect
import copy
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from engine import load_config, load_prices
from prop_sim import simulate_prop, rolling_start
from compliant_sim import Position

HERE = Path(__file__).parent
SOURCES = {
    "gary": "data/signals.jsonl",
    "goldscalperninja": "data/signals_goldscalperninja.jsonl",
}


def load_tagged(cfg, candles, times):
    """Load every channel's signals, tag with channel, attach entry index,
    apply the same filters as the engine. Returns one chronological list."""
    filt = cfg["filters"]
    pip = cfg["market"]["pip_value"]
    out = []
    for chan, path in SOURCES.items():
        for l in (HERE / path).open(encoding="utf-8"):
            s = json.loads(l)
            s["channel"] = chan
            if s["sl"] is None or s["tp_mode"] is None or not s["tp_raw"]:
                continue
            t = datetime.fromisoformat(s["date"])
            i = bisect.bisect_right(times, t)
            if i >= len(candles) or (candles[i][0] - t) > timedelta(days=3):
                continue
            entry = candles[i][1]
            sl_dist = abs(entry - s["sl"])
            if sl_dist > filt["max_sl_dollars"] or sl_dist / pip < filt.get("min_sl_pips", 0):
                continue
            s["_idx"] = i
            out.append(s)
    out.sort(key=lambda s: (s["date"], s["_idx"]))   # chronological, FCFS
    return out


def run_merged(cfg, candles, sigs):
    """Event loop: one open position max, FCFS, drop signals while busy."""
    open_pos = None
    closed = []
    dropped = defaultdict(int)
    occ = 0
    # bucket signals by entry candle index for fast lookup, preserving FCFS order
    by_idx = defaultdict(list)
    for s in sigs:
        by_idx[s["_idx"]].append(s)

    for i, c in enumerate(candles):
        for s in by_idx.get(i, []):
            if open_pos is not None:
                dropped[s["channel"]] += 1
                continue
            open_pos = Position(s, c[1], cfg)
            open_pos._channel = s["channel"]
        if open_pos is not None:
            occ += 1
            r = open_pos.step(c)
            if r:
                r["channel"] = open_pos._channel
                closed.append(r)
                open_pos = None
    if open_pos is not None:
        r = open_pos.close_at(candles[-1][4]); r["channel"] = open_pos._channel
        closed.append(r)
    closed.sort(key=lambda t: t["date"])
    return closed, dropped, occ


def stats(trades):
    n = len(trades)
    if not n:
        return None
    w = sum(1 for t in trades if t["R"] > 0)
    totR = sum(t["R"] for t in trades)
    eq = pk = mdd = 0.0
    for t in trades:
        eq += t["R"]; pk = max(pk, eq); mdd = min(mdd, eq - pk)
    gl = -sum(t["R"] for t in trades if t["R"] < 0) or 1e-9
    return {"n": n, "win": w / n * 100, "R": totR, "pct": totR * 0.5,
            "pf": sum(t["R"] for t in trades if t["R"] > 0) / gl, "mdd": mdd}


def line(label, s):
    if not s:
        print(f"  {label:22} no trades"); return
    print(f"  {label:22} n={s['n']:3} win={s['win']:4.1f}% R={s['R']:+7.2f} "
          f"({s['pct']:+5.1f}%) PF={s['pf']:.2f} maxDD={s['mdd']:.1f}R")


def main():
    cfg = load_config()
    candles = load_prices(cfg)
    times = [c[0] for c in candles]

    print("=" * 74)
    print("MERGED SINGLE-ACCOUNT SIM  —  Gary + GoldScalperNinja")
    print("  one trade at a time, first-come-first-served, Strategy D, 0.5% risk")
    print("=" * 74)

    sigs = load_tagged(cfg, candles, times)
    elig = defaultdict(int)
    for s in sigs:
        elig[s["channel"]] += 1
    print(f"\nEligible signals after filters: {dict(elig)}  (total {len(sigs)})")

    closed, dropped, occ = run_merged(cfg, candles, sigs)

    print(f"\n[OCCUPANCY] in a trade {occ/len(candles)*100:.1f}% of the time")
    print(f"[DROPPED while busy] {dict(dropped)}  (total {sum(dropped.values())})")

    print("\n[RESULT] merged stream:")
    line("MERGED (both)", stats(closed))
    line("  from gary", stats([t for t in closed if t["channel"] == "gary"]))
    line("  from goldscalperninja", stats([t for t in closed if t["channel"] == "goldscalperninja"]))

    # ---- prop sim on the merged trade stream ----
    p = cfg["prop"]
    print("\n[PROP-FIRM] 0.5% risk, 2% self-stop, static -10% / -4% daily reset 21:00 UTC")
    for target, ph in [(8.0, "Phase1 +8%"), (6.0, "Phase2 +6%")]:
        rep = simulate_prop(closed, p["risk_per_trade_pct"], p["daily_loss_stop_pct"],
                            p["firm_daily_dd_pct"], p["firm_max_dd_pct"], target,
                            p["max_dd_mode"], p["day_reset_utc_hour"])
        st = "BREACH/FAIL" if rep["breach"] else ("PASS" if rep["target_day"] else "not reached")
        extra = (f"hit on {rep['target_day']} in {rep['days_traded']}d/{rep['trades_taken']}t"
                 if rep["target_day"] else f"final {rep['final_equity']:+.2f}%")
        print(f"  {ph:11} {st:12} {extra}")
        print(f"              worst day {rep['worst_day']:.2f}%  maxDD {rep['max_dd']:.2f}%"
              + (f"  >>> {rep['breach']}" if rep["breach"] else ""))

    print("\n[ROLLING-START] start the challenge on every trading day:")
    import statistics as stx
    for target, ph in [(8.0, "P1"), (6.0, "P2")]:
        r = rolling_start(cfg, closed, target)
        done = r["passes"] + r["fails"]
        pr = (r["passes"] / done * 100) if done else 0
        dtp = r["days_to_pass"]
        md = f"med {stx.median(dtp):.0f}/max {max(dtp)}" if dtp else "—"
        print(f"  {ph}(+{target:.0f}%): {r['passes']}/{done} pass ({pr:.0f}%), "
              f"{r['fails']} breach, {r['incompletes']} ran-out | days-to-pass {md}")
        for s, b in r["breach_examples"]:
            print(f"      breach if started {s}: {b}")


if __name__ == "__main__":
    main()
