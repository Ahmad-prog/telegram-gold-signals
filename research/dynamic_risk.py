"""
Dynamic per-trade risk ladder on the F1 config (merged A, sells, fresh, SL 40-120p).

User's scheme: rung ladder e.g. [2.0, 1.5, 1.0]%.
  - a losing trade moves DOWN one rung (stays at last rung after more losses)
  - any winning trade resets to rung 0
  - rung state persists across days; daily stop = 2 consecutive losses/day
Evaluated as rolling two-phase (P1 +8% -> P2 +6%), firm -4% daily / -10% static,
equity accrued per-trade in % (exact intraday accounting, no constant-risk shortcut).

    python3 research/dynamic_risk.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep_lib as SL
from verify import load_rows, apply_filters, fcfs_filter
from days_detail import F1, TEST_CUT


def build_day_trades(trades):
    """Group trades into session days: list of (day_epoch, [R,...])."""
    days = []
    cur = None
    for t in trades:
        d = SL.session_day(t["epoch"])
        if not days or days[-1][0] != d:
            days.append((d, []))
        days[-1][1].append(t["R"])
    return days


def two_phase_ladder(day_trades, s, rungs, consec_stop=2,
                     tgt1=8.0, tgt2=6.0, firm_daily=4.0, firm_max=10.0):
    """Returns (outcome, p1_days, p2_days, worst_day_pct)."""
    eq = 0.0
    phase, tgt = 1, tgt1
    rung = 0
    p1_days = 0
    worst_day = 0.0
    n = len(day_trades)
    for i in range(s, n):
        day_pnl = 0.0
        consec = 0
        for R in day_trades[i][1]:
            if consec >= consec_stop:
                continue                      # daily stop hit -> skip rest
            risk = rungs[rung]
            pnl = R * risk
            day_pnl += pnl
            # firm checks at trade granularity (intraday running totals)
            if day_pnl <= -firm_daily:
                return "breach", (i - s + 1 if phase == 1 else p1_days), \
                       (0 if phase == 1 else i - s + 1 - p1_days), min(worst_day, day_pnl)
            if (eq + day_pnl) <= -firm_max:
                return "breach", (i - s + 1 if phase == 1 else p1_days), \
                       (0 if phase == 1 else i - s + 1 - p1_days), min(worst_day, day_pnl)
            if R > 0:
                rung = 0
                consec = 0
            else:
                rung = min(rung + 1, len(rungs) - 1)
                consec += 1
            # target check intra-day
            if eq + day_pnl >= tgt:
                if phase == 1:
                    p1_days = i - s + 1
                    phase, tgt = 2, tgt2
                    eq = -day_pnl             # so eq+day_pnl = 0 for new phase
                else:
                    return "pass", p1_days, (i - s + 1 - p1_days), worst_day
        eq += day_pnl
        worst_day = min(worst_day, day_pnl)
    return "incomplete", 0, 0, worst_day


def evaluate(trades, rungs, label, full_table=False):
    dts = build_day_trades(trades)
    dk_ep = np.array([d * 86400.0 - (24 - SL.RESET_HOUR) * 3600 for d, _ in dts])
    for name, msk in (("TEST", dk_ep >= TEST_CUT), ("FULL", np.ones_like(dk_ep, bool))):
        idxs = np.where(msk)[0]
        sub = [dts[i] for i in idxs]
        outs = []
        for s in range(len(sub)):
            outs.append((sub[s][0], *two_phase_ladder(sub, s, rungs)))
        passes = [o for o in outs if o[1] == "pass"]
        breaches = [o for o in outs if o[1] == "breach"]
        res = len(passes) + len(breaches)
        tot = sorted(o[2] + o[3] for o in passes)
        wd = min((o[4] for o in outs), default=0)
        if tot:
            q = lambda p: tot[min(len(tot) - 1, int(p * len(tot)))]
            dist = f"min={tot[0]} p25={q(.25)} med={q(.5)} p75={q(.75)} max={tot[-1]}"
        else:
            dist = "—"
        print(f"  {label:34} {name}: {len(passes)}/{res} ({len(passes)/res*100 if res else 0:3.0f}%) "
              f"days[{dist}] worstDay={wd:+.1f}%")
        if full_table and name == "FULL":
            print(f"    {'start':12}{'outcome':11}{'P1':>5}{'P2':>5}{'TOT':>6}")
            for ep, o, p1, p2, _ in outs:
                d = datetime.fromtimestamp(ep * 86400.0 - (24 - SL.RESET_HOUR) * 3600
                                           if ep < 1e9 else ep, tz=timezone.utc)
                d = datetime.fromtimestamp(dk_ep[0], tz=timezone.utc)  # placeholder
            # print compactly instead:
            row = []
            for ep_day, o, p1, p2, _ in outs:
                dd = datetime.fromtimestamp(ep_day * 86400.0 - (24 - SL.RESET_HOUR) * 3600,
                                            tz=timezone.utc).strftime("%b%d")
                row.append(f"{dd}→{p1}+{p2}={p1+p2}" if o == "pass"
                           else f"{dd}→{'BREACH' if o=='breach' else 'n/a'}")
            for i in range(0, len(row), 5):
                print("    " + "  ".join(f"{x:18}" for x in row[i:i + 5]))


def main():
    print("=" * 100)
    print("DYNAMIC RISK LADDER — F1 (merged A, sells, fresh, SL 40-120p), FCFS, consec-2 daily stop")
    print("=" * 100)
    for tag, lbl in (("c3", "3p cost"), ("c4_s0.5", "STRESS 4p+0.5slip")):
        rows = load_rows("merged", "A", tag)
        filt, _, _ = apply_filters(rows, F1)
        filt, _ = fcfs_filter(filt)
        print(f"\n--- {lbl} ---")
        evaluate(filt, [2.0, 1.5, 1.0], "LADDER 2.0/1.5/1.0 (user)")
        evaluate(filt, [1.75, 1.25, 1.0], "ladder 1.75/1.25/1.0")
        evaluate(filt, [1.5, 1.25, 1.0], "ladder 1.5/1.25/1.0")
        evaluate(filt, [1.5, 1.0, 0.75], "ladder 1.5/1.0/0.75")
        evaluate(filt, [1.25], "fixed 1.25 (baseline)")
        evaluate(filt, [1.5], "fixed 1.5 (baseline)")

    # full table for the user's ladder at 3p
    rows = load_rows("merged", "A", "c3")
    filt, _, _ = apply_filters(rows, F1)
    filt, _ = fcfs_filter(filt)
    print("\n" + "=" * 100)
    print("EVERY START — user ladder 2.0/1.5/1.0, 3p cost, FULL YEAR")
    print("=" * 100)
    evaluate(filt, [2.0, 1.5, 1.0], "LADDER 2.0/1.5/1.0", full_table=True)


if __name__ == "__main__":
    main()
