"""
STRESS TEST — sweep every parameter combination to find the best AND most
ROBUST way to trade GARY GOLD TRADER signals, and to confirm the base result
is not a fragile artifact.

    python stress_test.py

Sweeps:  exit strategy (A/B/C) x round-trip cost x slippage x entry mode x
         same-candle tie rule.  (ranges come from parameters.yml -> stress)
Outputs: data/stress_results.csv (one row per combo) + console analysis:
   1. Strategy showdown at the base config
   2. Cost sensitivity curve
   3. Worst-case robustness (pessimistic ties + high cost + slippage)
   4. Best & most-robust combos
   5. Breakdown of the base config: by month, by side, by add-on
"""

import copy
import csv
import itertools
from collections import defaultdict
from pathlib import Path

from engine import load_config, load_prices, load_signals, run_backtest

HERE = Path(__file__).parent


def with_overrides(cfg, strategy=None, cost=None, slip=None, entry=None, tie=None):
    c = copy.deepcopy(cfg)
    if strategy is not None: c["exit"]["strategy"] = strategy
    if cost is not None:     c["market"]["round_trip_cost_pips"] = cost
    if slip is not None:     c["market"]["slippage_pips"] = slip
    if entry is not None:    c["entry"]["mode"] = entry
    if tie is not None:      c["exit"]["same_candle_tie"] = tie
    return c


def row(label, r):
    if r["n"] == 0:
        return f"{label:42} no trades"
    return (f"{label:42} n={r['n']:3}  win={r['win_rate']:4.1f}%  "
            f"R={r['total_R']:+7.2f}  {r['total_pct']:+6.1f}%  "
            f"PF={r['profit_factor']:.2f}  DD={r['max_dd_R']:6.2f}R")


def main():
    cfg = load_config()
    candles = load_prices(cfg)
    signals = load_signals(cfg)
    S = cfg["stress"]

    def bt(**kw):
        return run_backtest(with_overrides(cfg, **kw), candles, signals)

    # ---------------- full sweep -> CSV ----------------
    combos = list(itertools.product(
        S["strategies"], S["cost_pips"], S["slippage_pips"],
        S["entry_modes"], S["same_candle_tie"]))
    print(f"Running {len(combos)} parameter combinations...\n")
    rows = []
    for strat, cost, slip, entry, tie in combos:
        r = bt(strategy=strat, cost=cost, slip=slip, entry=entry, tie=tie)
        rows.append({
            "strategy": strat, "cost_pips": cost, "slippage_pips": slip,
            "entry_mode": entry, "tie": tie, "n": r["n"],
            "win_rate": round(r.get("win_rate", 0), 1),
            "total_R": round(r.get("total_R", 0), 2),
            "total_pct": round(r.get("total_pct", 0), 1),
            "profit_factor": round(r.get("profit_factor", 0), 2)
                if r["n"] else 0,
            "max_dd_R": round(r.get("max_dd_R", 0), 2),
        })
    out = HERE / cfg["paths"]["stress_out"]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(f"Full sweep ({len(rows)} combos) -> {out}\n")

    base_cost = cfg["market"]["round_trip_cost_pips"]

    # ---------------- 1. strategy showdown (base config) ----------------
    print("=" * 80)
    print(f"1) STRATEGY SHOWDOWN  (cost={base_cost}p, slip=0, tie=pessimistic, market_next)")
    print("=" * 80)
    for strat in ["A", "B", "C"]:
        r = bt(strategy=strat, cost=base_cost, slip=0.0,
               entry="market_next", tie="pessimistic")
        print(row(f"  Strategy {strat}", r))

    # ---------------- 2. cost sensitivity (strategy C) ----------------
    print("\n" + "=" * 80)
    print("2) COST SENSITIVITY  (strategy C, slip=0, pessimistic)")
    print("=" * 80)
    for cost in S["cost_pips"]:
        r = bt(strategy="C", cost=cost, slip=0.0,
               entry="market_next", tie="pessimistic")
        print(row(f"  round-trip {cost}p", r))

    # ---------------- 3. worst-case robustness ----------------
    print("\n" + "=" * 80)
    print("3) WORST-CASE ROBUSTNESS  (pessimistic ties, high cost, high slippage)")
    print("=" * 80)
    for strat in ["A", "B", "C"]:
        r = bt(strategy=strat, cost=4, slip=1.0,
               entry="market_next", tie="pessimistic")
        print(row(f"  Strategy {strat} @ 4p cost +1p slip", r))

    # ---------------- 3b. ENTRY MODE comparison (critical) ----------------
    print("\n" + "=" * 80)
    print("3b) ENTRY MODE: market-now vs wait-for-zone  (strategy C, cost=3p)")
    print("=" * 80)
    for em in ["market_next", "zone_touch"]:
        r = bt(strategy="C", cost=3, slip=0.0, entry=em, tie="pessimistic")
        print(row(f"  {em}", r))

    # ---------------- 4. best & most-robust combos ----------------
    print("\n" + "=" * 80)
    print("4) RANKINGS  (restricted to realistic market_next entry)")
    print("=" * 80)
    valid = [r for r in rows if r["n"] > 0]
    mn = [r for r in valid if r["entry_mode"] == "market_next"]
    best = sorted(mn, key=lambda r: r["total_R"], reverse=True)[:5]
    print("  Top 5 by total R:")
    for r in best:
        print(f"    {r['strategy']} cost={r['cost_pips']}p slip={r['slippage_pips']} "
              f"{r['tie']:11} -> R={r['total_R']:+.2f} win={r['win_rate']}% "
              f"PF={r['profit_factor']} DD={r['max_dd_R']}R")
    # risk-adjusted = total_R / |maxDD| — what matters for prop-firm survival
    print("\n  Best RISK-ADJUSTED (R per unit drawdown, cost=3p, pessimistic):")
    for strat in ["A", "B", "C"]:
        sub = [r for r in mn if r["strategy"] == strat and r["cost_pips"] == 3
               and r["tie"] == "pessimistic" and r["slippage_pips"] == 0.0]
        if sub:
            r = sub[0]
            dd = abs(r["max_dd_R"]) or 1e-9
            print(f"    Strategy {strat}: R={r['total_R']:+.2f}  DD={r['max_dd_R']}R  "
                  f"R/DD={r['total_R']/dd:.2f}  win={r['win_rate']}%")
    print("\n  Most robust worst-case (market_next, cost>=2p, pessimistic, any slip):")
    for strat in ["A", "B", "C"]:
        sub = [r for r in mn if r["strategy"] == strat
               and r["cost_pips"] >= 2 and r["tie"] == "pessimistic"]
        if sub:
            worst = min(sub, key=lambda r: r["total_R"])
            print(f"    Strategy {strat}: worst-case R={worst['total_R']:+.2f} "
                  f"(at cost={worst['cost_pips']}p slip={worst['slippage_pips']})")

    # ---------------- 5. breakdown of base config ----------------
    print("\n" + "=" * 80)
    print(f"5) BREAKDOWN  (base config: strategy={cfg['exit']['strategy']}, "
          f"cost={base_cost}p)")
    print("=" * 80)
    base = run_backtest(cfg, candles, signals)
    trades = base["trades"]

    def agg(group):
        n = len(group)
        if not n: return "—"
        wr = sum(1 for t in group if t["R"] > 0) / n * 100
        return (f"n={n:3}  win={wr:4.1f}%  R={sum(t['R'] for t in group):+7.2f}  "
                f"{sum(t['pct'] for t in group):+5.1f}%")

    by_month = defaultdict(list)
    for t in trades: by_month[t["date"][:7]].append(t)
    print("  By month:")
    for m in sorted(by_month): print(f"    {m}   {agg(by_month[m])}")

    print("  By side:")
    for side in ["buy", "sell"]:
        print(f"    {side:4}  {agg([t for t in trades if t['side']==side])}")

    print("  By type:")
    print(f"    fresh   {agg([t for t in trades if not t['addon']])}")
    print(f"    add-on  {agg([t for t in trades if t['addon']])}")


if __name__ == "__main__":
    main()
