"""
3-YEAR validation — merged Gary+GSN on the full ~3y history.
Uses data/xauusd_1m_3y.csv + the 3y signal files.

    python validate_3y.py
"""
import copy
from collections import defaultdict

from engine import load_config, load_prices
from prop_sim import simulate_prop, rolling_start, session_date
import merged_sim
from merged_sim import run_merged
from account_sim import simulate_real

# point the merged loader at the 3-year files
merged_sim.SOURCES = {
    "gary": "data/signals_gary_3y.jsonl",
    "goldscalperninja": "data/signals_gsn_3y.jsonl",
}


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
    return dict(n=n, win=w/n*100, R=totR, pct=totR*0.5,
                pf=sum(t["R"] for t in trades if t["R"] > 0)/gl, mdd=mdd)


def line(lbl, s):
    if not s:
        print(f"  {lbl:24} no trades"); return
    print(f"  {lbl:24} n={s['n']:4} win={s['win']:4.1f}% R={s['R']:+8.2f} "
          f"({s['pct']:+6.1f}%) PF={s['pf']:.2f} maxDD={s['mdd']:.1f}R")


def main():
    cfg = load_config()
    cfg["paths"]["prices"] = "data/xauusd_1m_3y.csv"
    candles = load_prices(cfg)
    times = [c[0] for c in candles]
    print("=" * 78)
    print("3-YEAR VALIDATION — merged Gary+GSN, 1-at-a-time FCFS, Strategy D")
    print(f"  prices {candles[0][0].date()} -> {candles[-1][0].date()} ({len(candles):,} bars)")
    print("=" * 78)

    sigs = merged_sim.load_tagged(cfg, candles, times)
    elig = defaultdict(int)
    for s in sigs:
        elig[s["channel"]] += 1
    print(f"\nEligible signals: {dict(elig)} (total {len(sigs)})")

    closed, dropped, occ = run_merged(cfg, candles, sigs)
    print(f"Occupancy {occ/len(candles)*100:.1f}% | dropped(busy) {dict(dropped)}\n")
    line("MERGED (both)", stats(closed))
    line("  gary", stats([t for t in closed if t["channel"] == "gary"]))
    line("  goldscalperninja", stats([t for t in closed if t["channel"] == "goldscalperninja"]))

    # by year / month
    print("\nBY YEAR:")
    byy = defaultdict(list)
    for t in closed:
        byy[t["date"][:4]].append(t)
    for y in sorted(byy):
        line(y, stats(byy[y]))

    print("\nBY MONTH:")
    bym = defaultdict(list)
    for t in closed:
        bym[t["date"][:7]].append(t)
    for m in sorted(bym):
        s = stats(bym[m])
        print(f"    {m}  n={s['n']:3} win={s['win']:4.1f}% R={s['R']:+7.2f} @0.5%={s['pct']:+5.1f}%")

    # prop + rolling start
    p = cfg["prop"]
    sm = cfg["account"].get("daily_stop_mode", "pct")
    nt = cfg["account"].get("daily_net_loss_stop", 2)
    ct = cfg["account"].get("daily_consec_loss_stop", 2)
    print(f"\nPROP-FIRM (0.5% risk, stop={sm}):")
    for target, ph in [(8.0, "Phase1 +8%"), (6.0, "Phase2 +6%")]:
        rep = simulate_prop(closed, 0.5, p["daily_loss_stop_pct"], p["firm_daily_dd_pct"],
                            p["firm_max_dd_pct"], target, p["max_dd_mode"],
                            p["day_reset_utc_hour"], stop_mode=sm, net_thr=nt, consec_thr=ct)
        st = "BREACH" if rep["breach"] else ("PASS" if rep["target_day"] else "not reached")
        print(f"  {ph:11} {st:10} worstDay {rep['worst_day']:.2f}% maxDD {rep['max_dd']:.2f}%")

    print("\nROLLING-START (every trading day, 0.5% risk):")
    import statistics as stx
    for target, ph in [(8.0, "P1"), (6.0, "P2")]:
        r = rolling_start(cfg, closed, target)
        done = r["passes"] + r["fails"]
        pr = (r["passes"]/done*100) if done else 0
        dtp = r["days_to_pass"]
        md = f"med {stx.median(dtp):.0f}/max {max(dtp)}" if dtp else "—"
        print(f"  {ph}(+{target:.0f}%): {r['passes']}/{done} pass ({pr:.0f}%), "
              f"{r['fails']} breach | days-to-pass {md}")

    # real account scaling
    print("\nREAL ACCOUNT (compounding, consec(2) stop):")
    for risk in [0.5, 1.0, 2.0, 5.0]:
        rep = simulate_real(closed, risk, True, 50.0, 2.0, p["day_reset_utc_hour"],
                            stop_mode=sm, net_thr=nt, consec_thr=ct)
        print(f"  {risk:>4}% : return {rep['final_pct']:+8.1f}%  maxDD {rep['max_dd']:6.1f}%  "
              f"worstDay {rep['worst_day']:.1f}%  blown {rep['blown_on'] or 'no'}")


if __name__ == "__main__":
    main()
