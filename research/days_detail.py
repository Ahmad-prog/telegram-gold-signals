"""
Days-to-pass deep dive for the top config (F1: merged A, sells, fresh, SL 40-120p).

1) Tests every speed lever: fixed risk ladder, consec-3 stop, phase-based risk,
   step-down risk (aggressive until cushion, then safe).
2) Prints the FULL per-start distribution (every evaluation start date, its
   outcome, P1 days, P2 days, total days) — no medians-only.

    python3 research/days_detail.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep_lib as SL
from verify import load_rows, apply_filters, fcfs_filter

F1 = {"session": "all", "side": "sell", "t4": "any", "t24": "any",
      "addon": "fresh", "slb": "mid40_120", "vol": "any"}
TEST_CUT = datetime.fromisoformat(SL.TRAIN_END).replace(tzinfo=timezone.utc).timestamp()


def two_phase_detail(dayR, intra, s, risk_fn, tgt1=8.0, tgt2=6.0,
                     firm_daily=4.0, firm_max=10.0):
    """risk_fn(phase, eq_pct) -> risk %. Returns (outcome, p1_days, p2_days)."""
    eq = 0.0
    phase = 1
    tgt = tgt1
    p1_days = 0
    n = len(dayR)
    for i in range(s, n):
        risk = risk_fn(phase, eq)
        if intra[i] * risk <= -firm_daily:
            return "breach", (i - s + 1 if phase == 1 else p1_days), \
                   (0 if phase == 1 else i - s + 1 - p1_days)
        if (eq + intra[i]) * risk <= -firm_max:
            return "breach", (i - s + 1 if phase == 1 else p1_days), \
                   (0 if phase == 1 else i - s + 1 - p1_days)
        # dayR is in R units; equity tracked in % (risk applied at day start,
        # so a step-down scheme changes size between days, not mid-day)
        eq += dayR[i] * risk
        if eq >= tgt:
            if phase == 1:
                p1_days = i - s + 1
                phase = 2; tgt = tgt2; eq = 0.0
            else:
                return "pass", p1_days, (i - s + 1 - p1_days)
    return "incomplete", 0, 0


def eval_scheme(trades, thr, risk_fn, label, windows=("te", "full")):
    sub = [{"epoch": t["epoch"], "R": t["R"]} for t in trades]
    dk, dayR, intra = SL.day_aggregate(sub, "consec", thr)
    dk_ep = dk * 86400.0 - (24 - SL.RESET_HOUR) * 3600
    out = {}
    for name in windows:
        msk = (dk_ep >= TEST_CUT) if name == "te" else np.ones_like(dk_ep, bool)
        idxs = np.where(msk)[0]
        dR, di = dayR[idxs[0]:idxs[-1] + 1], intra[idxs[0]:idxs[-1] + 1]
        dep = dk_ep[idxs[0]:idxs[-1] + 1]
        rows = []
        for s in range(len(dR)):
            o, p1, p2 = two_phase_detail(dR, di, s, risk_fn)
            rows.append((dep[s], o, p1, p2))
        out[name] = rows
    # summary
    parts = []
    for name in windows:
        rows = out[name]
        passes = [r for r in rows if r[1] == "pass"]
        breaches = [r for r in rows if r[1] == "breach"]
        res = len(passes) + len(breaches)
        tot = sorted(r[2] + r[3] for r in passes)
        if tot:
            q = lambda p: tot[min(len(tot) - 1, int(p * len(tot)))]
            dist = f"min={tot[0]} p25={q(.25)} med={q(.5)} p75={q(.75)} max={tot[-1]}"
        else:
            dist = "—"
        parts.append(f"{name.upper()}: {len(passes)}/{res} pass "
                     f"({len(passes)/res*100 if res else 0:.0f}%) days[{dist}]")
    print(f"  {label:46} {' | '.join(parts)}")
    return out


def main():
    rows = load_rows("merged", "A", "c3")
    filt, _, _ = apply_filters(rows, F1)
    filt, _ = fcfs_filter(filt)
    rows4 = load_rows("merged", "A", "c4_s0.5")
    filt4, _, _ = apply_filters(rows4, F1)
    filt4, _ = fcfs_filter(filt4)

    fixed = lambda r: (lambda ph, eq: r)
    stepdown = lambda hi, lo, cush: (lambda ph, eq: hi if eq < cush else lo)
    phased = lambda r1, r2: (lambda ph, eq: r1 if ph == 1 else r2)

    print("=" * 100)
    print("SPEED LEVERS  (F1: merged A, sells, fresh, SL 40-120p — FCFS, 3p cost)")
    print("=" * 100)
    for thr in (2, 3):
        for lbl, fn in [
            ("fixed 1.25%", fixed(1.25)),
            ("fixed 1.5%", fixed(1.5)),
            ("fixed 1.75%", fixed(1.75)),
            ("fixed 2.0%", fixed(2.0)),
            ("step-down 2.0%->1.25% @+3% cushion", stepdown(2.0, 1.25, 3.0)),
            ("step-down 1.75%->1.25% @+3% cushion", stepdown(1.75, 1.25, 3.0)),
            ("phase P1=1.5% P2=1.25%", phased(1.5, 1.25)),
        ]:
            eval_scheme(filt, thr, fn, f"consec{thr} | {lbl}")
        print()

    print("=" * 100)
    print("SAME LEVERS UNDER STRESS (4p cost + 0.5p slip)")
    print("=" * 100)
    for lbl, fn in [
        ("fixed 1.25%", fixed(1.25)),
        ("fixed 1.5%", fixed(1.5)),
        ("step-down 1.75%->1.25% @+3% cushion", stepdown(1.75, 1.25, 3.0)),
    ]:
        eval_scheme(filt4, 2, fn, f"consec2 | {lbl}")

    # ---- FULL per-start table for the chosen configs ----
    for lbl, fn, thr in [("RISK 1.25% consec2 (RECOMMENDED)", fixed(1.25), 2),
                         ("STEP-DOWN 1.75->1.25 @+3% consec2", stepdown(1.75, 1.25, 3.0), 2)]:
        print("\n" + "=" * 100)
        print(f"EVERY EVALUATION START — {lbl} — FULL YEAR (3p cost)")
        print("=" * 100)
        out = eval_scheme(filt, thr, fn, lbl, windows=("full",))
        rows_ = out["full"]
        print(f"{'start date':12} {'outcome':11} {'P1 days':>8} {'P2 days':>8} {'TOTAL':>7}")
        for ep, o, p1, p2 in rows_:
            d = datetime.fromtimestamp(ep, tz=timezone.utc).date().isoformat()
            if o == "pass":
                print(f"{d:12} {o:11} {p1:8} {p2:8} {p1+p2:7}")
            elif o == "breach":
                print(f"{d:12} {o:11} {'—':>8} {'—':>8} {'—':>7}")
            else:
                print(f"{d:12} {o:11} {'(ran out of data)':>26}")


if __name__ == "__main__":
    main()
