"""
Full validation for one channel's signals, mirroring what we ran for Gary:
  - backtest at the LOCKED config (Strategy D, market_next, 3p cost, 0.5% risk)
  - strategy showdown (A / C / D)
  - prop-firm pass/fail + rolling-start robustness
  - occupancy + result under a 1-trade-at-a-time cap
  - side / month breakdown

    python validate_channel.py data/signals_goldscalperninja.jsonl
"""

import copy
import sys
from collections import defaultdict
from datetime import datetime

from engine import load_config, load_prices, run_backtest
from prop_sim import simulate_prop, rolling_start, session_date
from compliant_sim import prep_signals, Position

SIG = sys.argv[1] if len(sys.argv) > 1 else "data/signals_goldscalperninja.jsonl"


def line(label, r):
    if r["n"] == 0:
        print(f"  {label:14} no trades"); return
    print(f"  {label:14} n={r['n']:3} win={r['win_rate']:4.1f}% "
          f"R={r['total_R']:+7.2f} ({r['total_pct']:+5.1f}%) "
          f"PF={r['profit_factor']:.2f} DD={r['max_dd_R']:6.2f}R")


def main():
    cfg = load_config()
    cfg["paths"]["signals"] = SIG
    candles = load_prices(cfg)
    times = [c[0] for c in candles]

    print("=" * 72)
    print(f"CHANNEL VALIDATION  ->  {SIG}")
    print(f"  locked: strat {cfg['exit']['strategy']}, {cfg['entry']['mode']}, "
          f"cost {cfg['market']['round_trip_cost_pips']}p, "
          f"risk {cfg['risk']['risk_per_trade_pct']}%")
    print("=" * 72)

    base = run_backtest(cfg)
    print("\n[1] LOCKED-CONFIG BACKTEST (independent trades)")
    line("locked D", base)
    print(f"     skipped {base['skipped']} | outcomes {base['outcomes']}")

    print("\n[2] STRATEGY SHOWDOWN (market_next, 3p, 0.5%)")
    for s in ["A", "B", "C", "D"]:
        c = copy.deepcopy(cfg); c["exit"]["strategy"] = s
        line(f"strategy {s}", run_backtest(c))

    print("\n[3] ENTRY MODE (strategy D)")
    for em in ["market_next", "zone_touch"]:
        c = copy.deepcopy(cfg); c["entry"]["mode"] = em
        line(em, run_backtest(c))

    print("\n[4] COST SENSITIVITY (strategy D, market_next)")
    for cost in [0, 1, 2, 3, 4, 5]:
        c = copy.deepcopy(cfg); c["market"]["round_trip_cost_pips"] = cost
        line(f"cost {cost}p", run_backtest(c))

    # ---- prop sim ----
    trades = base["trades"]
    p = cfg["prop"]
    print("\n[5] PROP-FIRM SIM (0.5% risk, 2% self-stop, static -10% / -4% daily)")
    for target, ph in [(8.0, "Phase1 +8%"), (6.0, "Phase2 +6%")]:
        rep = simulate_prop(trades, p["risk_per_trade_pct"], p["daily_loss_stop_pct"],
                            p["firm_daily_dd_pct"], p["firm_max_dd_pct"], target,
                            p["max_dd_mode"], p["day_reset_utc_hour"])
        st = "BREACH/FAIL" if rep["breach"] else ("PASS" if rep["target_day"] else "not reached")
        extra = (f"in {rep['days_traded']}d/{rep['trades_taken']}t" if rep["target_day"]
                 else f"final {rep['final_equity']:+.2f}%")
        print(f"  {ph:11} {st:12} {extra}  worstDay {rep['worst_day']:.2f}% "
              f"maxDD {rep['max_dd']:.2f}%" + (f"  >>>{rep['breach']}" if rep["breach"] else ""))

    print("\n[6] ROLLING-START ROBUSTNESS (start on every trading day)")
    for target, ph in [(8.0, "P1"), (6.0, "P2")]:
        r = rolling_start(cfg, trades, target)
        done = r["passes"] + r["fails"]
        pr = (r["passes"] / done * 100) if done else 0
        import statistics as stx
        dtp = r["days_to_pass"]
        md = f"med {stx.median(dtp):.0f}/max {max(dtp)}" if dtp else "—"
        print(f"  {ph}(+{target:.0f}%): {r['passes']}/{done} pass ({pr:.0f}%), "
              f"{r['fails']} breach, {r['incompletes']} ran-out | days-to-pass {md}")

    # ---- 1-trade-at-a-time occupancy ----
    print("\n[7] ONE-TRADE-AT-A-TIME (max_concurrent=1)")
    c = copy.deepcopy(cfg); c["compliance"]["max_concurrent"] = 1
    sigs = prep_signals(c, candles, times)
    open_pos, closed, dropped, occ = [], [], 0, 0
    si = 0
    for i, cd in enumerate(candles):
        while si < len(sigs) and sigs[si]["_idx"] == i:
            s = sigs[si]; si += 1
            if open_pos:
                dropped += 1; continue
            open_pos.append(Position(s, cd[1], c))
        if open_pos:
            occ += 1; still = []
            for pos in open_pos:
                r = pos.step(cd); (closed.append(r) if r else still.append(pos))
            open_pos = still
    for pos in open_pos:
        closed.append(pos.close_at(candles[-1][4]))
    if closed:
        n = len(closed); w = sum(1 for t in closed if t["R"] > 0)
        totR = sum(t["R"] for t in closed)
        eq = pk = mdd = 0.0
        for t in sorted(closed, key=lambda t: t["date"]):
            eq += t["R"]; pk = max(pk, eq); mdd = min(mdd, eq - pk)
        print(f"  eligible {len(sigs)} | taken {n} | dropped(busy) {dropped} "
              f"({dropped/max(1,len(sigs))*100:.0f}%) | occupancy {occ/len(candles)*100:.1f}%")
        print(f"  result: win {w/n*100:.1f}%  {totR:+.1f}R ({totR*0.5:+.1f}% acct)  maxDD {mdd:.1f}R")

    # ---- breakdowns ----
    print("\n[8] BREAKDOWN (locked D)")
    def agg(g):
        if not g: return "—"
        n = len(g); w = sum(1 for t in g if t["R"] > 0)
        return f"n={n:3} win={w/n*100:4.1f}% R={sum(t['R'] for t in g):+7.2f}"
    print("  by side:")
    for s in ["buy", "sell"]:
        print(f"    {s:4} {agg([t for t in trades if t['side']==s])}")
    print("  by month:")
    bym = defaultdict(list)
    for t in trades: bym[t["date"][:7]].append(t)
    for m in sorted(bym): print(f"    {m} {agg(bym[m])}")


if __name__ == "__main__":
    main()
