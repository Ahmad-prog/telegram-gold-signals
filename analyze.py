"""
Deep loss/accuracy analysis for the LOCKED strategy (parameters.yml).

Answers: how many hit SL, WHY (did price go in favor first or straight against?),
and which conditions (side / session / hour / weekday / month / add-on / trend)
drive the losses — then tests concrete improvements (filters) on R and win rate.

    python analyze.py
"""

import bisect
import copy
from collections import defaultdict
from datetime import datetime, timezone

from engine import load_config, load_prices, load_signals, run_backtest

PIP = 0.10


def session(hour):
    if 0 <= hour < 7:   return "Asia (00-07)"
    if 7 <= hour < 13:  return "London (07-13)"
    if 13 <= hour < 21: return "NY (13-21)"
    return "Late (21-24)"


def wr(group):
    n = len(group)
    if not n:
        return "—"
    w = sum(1 for t in group if t["R"] > 0)
    tot = sum(t["R"] for t in group)
    return f"n={n:3}  win={w/n*100:4.1f}%  R={tot:+7.2f}  avg={tot/n:+.3f}R"


def main():
    cfg = load_config()
    candles = load_prices(cfg)
    times = [c[0] for c in candles]
    res = run_backtest(cfg)
    trades = res["trades"]

    print("=" * 72)
    print(f"DEEP ANALYSIS — strategy {cfg['exit']['strategy']}, "
          f"risk {cfg['risk']['risk_per_trade_pct']}%, cost "
          f"{cfg['market']['round_trip_cost_pips']}p")
    print("=" * 72)
    n = len(trades)
    losers = [t for t in trades if t["R"] < 0]
    winners = [t for t in trades if t["R"] > 0]
    print(f"Trades {n} | winners {len(winners)} | losers {len(losers)} | "
          f"win rate {len(winners)/n*100:.1f}%")

    # ---- outcome table with avg R ----
    print("\n--- OUTCOMES (avg R, avg pips) ---")
    byo = defaultdict(list)
    for t in trades:
        byo[t["outcome"]].append(t)
    for o in sorted(byo, key=lambda k: -len(byo[k])):
        g = byo[o]
        print(f"  {o:14} n={len(g):3}  avgR={sum(x['R'] for x in g)/len(g):+.3f}  "
              f"avgPips={sum(x['pips'] for x in g)/len(g):+.1f}")

    # ---- WHY SLs happen: max favorable excursion before the stop ----
    print("\n--- WHY SL HITS: how far price went IN FAVOR before stopping ---")
    near = straight = mid = 0
    mfes = []
    for t in losers:
        ts = datetime.fromisoformat(t["date"])
        i = bisect.bisect_right(times, ts)
        entry = t["entry"]; sl = t["sl"]; long = t["side"] == "buy"
        tp1_dist = abs(t["tp1"] - entry) / PIP
        mfe = 0.0
        for c in candles[i:]:
            fav = (c[2] - entry) if long else (entry - c[3])   # high/low in favor
            mfe = max(mfe, fav / PIP)
            hit_sl = (c[3] <= sl) if long else (c[2] >= sl)
            if hit_sl:
                break
        mfes.append(mfe)
        frac = mfe / tp1_dist if tp1_dist else 0
        if frac < 0.25:   straight += 1
        elif frac < 0.75: mid += 1
        else:             near += 1
    if mfes:
        am = sum(mfes) / len(mfes)
        print(f"  avg favorable move before SL: {am:.1f} pips "
              f"(TP1 is ~{sum(abs(t['tp1']-t['entry'])/PIP for t in losers)/len(losers):.0f} pips away)")
        print(f"  went STRAIGHT to SL (<25% toward TP1) : {straight}  "
              f"({straight/len(losers)*100:.0f}%)  -> entry/direction just wrong")
        print(f"  reversed MIDWAY (25-75% toward TP1)    : {mid}  "
              f"({mid/len(losers)*100:.0f}%)")
        print(f"  NEAR-MISS (>75% toward TP1) then SL    : {near}  "
              f"({near/len(losers)*100:.0f}%)  -> a tighter TP1 would save these")

    # ---- breakdowns ----
    def breakdown(title, keyfn):
        print(f"\n--- BY {title} ---")
        g = defaultdict(list)
        for t in trades:
            g[keyfn(t)].append(t)
        for k in sorted(g):
            print(f"  {str(k):16} {wr(g[k])}")

    breakdown("SIDE", lambda t: t["side"])
    breakdown("SESSION", lambda t: session(datetime.fromisoformat(t["date"]).hour))
    breakdown("WEEKDAY", lambda t: datetime.fromisoformat(t["date"]).strftime("%a"))
    breakdown("MONTH", lambda t: t["date"][:7])
    breakdown("ADD-ON", lambda t: "add-on" if t["addon"] else "fresh")
    breakdown("SL DISTANCE", lambda t: f"{int(abs(t['entry']-t['sl'])/PIP)//25*25}-{int(abs(t['entry']-t['sl'])/PIP)//25*25+25}p")

    # ---- consecutive-loss streaks ----
    streak = mx = 0
    for t in trades:
        streak = streak + 1 if t["R"] < 0 else 0
        mx = max(mx, streak)
    print(f"\nMax consecutive losing trades: {mx}")

    # ============== IMPROVEMENT EXPERIMENTS ==============
    print("\n" + "=" * 72)
    print("IMPROVEMENT EXPERIMENTS  (effect on total R and win rate)")
    print("=" * 72)
    base_R = sum(t["R"] for t in trades)
    print(f"  BASELINE (all {n} trades): {wr(trades)}")

    def show(label, subset):
        print(f"  {label:34} {wr(subset)}")

    show("Buys only", [t for t in trades if t["side"] == "buy"])
    show("Sells only", [t for t in trades if t["side"] == "sell"])
    show("Skip add-ons", [t for t in trades if not t["addon"]])
    show("London+NY only (07-21 UTC)",
         [t for t in trades if 7 <= datetime.fromisoformat(t["date"]).hour < 21])
    show("Skip Asia session",
         [t for t in trades if not (0 <= datetime.fromisoformat(t["date"]).hour < 7)])

    # ---- trend filter: 4h momentum at signal time ----
    LB = 240   # 4 hours of 1-min bars
    aligned, counter = [], []
    for t in trades:
        ts = datetime.fromisoformat(t["date"])
        i = bisect.bisect_right(times, ts)
        if i - LB < 0:
            continue
        past = candles[i - LB][4]
        now = candles[i][1]
        up = now > past
        if (t["side"] == "buy" and up) or (t["side"] == "sell" and not up):
            aligned.append(t)
        else:
            counter.append(t)
    print("\n  --- 4h-trend filter (take only signals WITH the 4h trend) ---")
    show("Trend-ALIGNED only", aligned)
    show("Counter-trend (what to drop)", counter)


if __name__ == "__main__":
    main()
