"""
Stage 4: adversarial verification of top candidate configs.

    python3 research/verify.py fcfs       # 1-trade-at-a-time FCFS realism
    python3 research/verify.py stress     # cost 4p + slippage 0.5p re-eval
    python3 research/verify.py regime3y   # how the config did 2023-2025 (red-flag)
    python3 research/verify.py neighbors  # OOS stability of neighbor configs

Top configs under test are defined in TOP below (from deep_eval ranking).
Each mode prints a compact report to stdout.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep_lib as SL

TRADES = SL.REPO / "research" / "trades"

# (channel, variant, filters, risk, stop_thr) — diverse picks from deep_eval top
TOP = [
    ("merged", "A",       {"session": "all",    "side": "sell", "t4": "any",
                           "t24": "any", "addon": "fresh", "slb": "mid40_120", "vol": "any"}, 1.5, 2),
    ("merged", "D5_noBE", {"session": "noasia", "side": "sell", "t4": "any",
                           "t24": "any", "addon": "fresh", "slb": "any", "vol": "any"}, 1.5, 2),
    ("merged", "D5_noBE", {"session": "ldnny",  "side": "sell", "t4": "any",
                           "t24": "any", "addon": "any", "slb": "any", "vol": "hi"}, 1.5, 2),
    ("merged", "D8",      {"session": "all",    "side": "sell", "t4": "aligned",
                           "t24": "any", "addon": "fresh", "slb": "mid40_120", "vol": "any"}, 1.5, 2),
    ("merged", "D5",      {"session": "ldnny",  "side": "sell", "t4": "aligned",
                           "t24": "any", "addon": "fresh", "slb": "any", "vol": "any"}, 1.5, 2),
    ("gary",   "D5_noBE", {"session": "all",    "side": "sell", "t4": "any",
                           "t24": "any", "addon": "any", "slb": "any", "vol": "hi"}, 1.5, 2),
]


def load_rows(chan, variant, tag="c3"):
    if chan == "merged":
        return sorted(load_rows("gary", variant, tag) + load_rows("gsn", variant, tag),
                      key=lambda r: r["epoch"])
    p = TRADES / f"{chan}__{variant}__{tag}.jsonl"
    return [json.loads(l) for l in p.open()]


def apply_filters(rows, f):
    tr = SL.trades_to_arrays(rows)
    masks = SL.build_masks(tr)
    m = (masks["session"][f["session"]] & masks["side"][f["side"]]
         & masks["t4"][f["t4"]] & masks["t24"][f["t24"]]
         & masks["addon"][f["addon"]] & masks["slb"][f["slb"]]
         & masks["vol"][f["vol"]])
    idx = np.where(m)[0]
    order = np.argsort(tr["epoch"])  # arrays already sorted, keep safe
    keep = set(idx.tolist())
    out = []
    srt = sorted(range(len(rows)), key=lambda i: rows[i]["epoch"])
    arr_i = 0
    # trades_to_arrays sorted by epoch: map mask back to sorted rows
    rows_sorted = [rows[i] for i in srt]
    return [rows_sorted[i] for i in idx], tr, m


def fcfs_filter(trades):
    """1-trade-at-a-time first-come-first-served: drop any trade whose entry
    is before the current open trade's exit."""
    out, dropped = [], 0
    busy_until = -1.0
    for t in trades:
        if t["epoch"] < busy_until:
            dropped += 1
            continue
        out.append(t)
        busy_until = t.get("exit_epoch") or t["epoch"]
    return out, dropped


def eval_windows(trades, risk, thr, label=""):
    test_cut = datetime.fromisoformat(SL.TRAIN_END).replace(tzinfo=timezone.utc).timestamp()
    sub = [{"epoch": t["epoch"], "R": t["R"]} for t in trades]
    dk, dayR, intra = SL.day_aggregate(sub, "consec", thr)
    dk_ep = dk * 86400.0 - (24 - SL.RESET_HOUR) * 3600
    res = {}
    for name, msk in (("train", dk_ep < test_cut), ("test", dk_ep >= test_cut),
                      ("full", np.ones_like(dk_ep, bool))):
        idxs = np.where(msk)[0]
        if len(idxs) == 0:
            res[name] = None; continue
        rep = SL.rolling_two_phase(dayR[idxs[0]:idxs[-1] + 1],
                                   intra[idxs[0]:idxs[-1] + 1], risk)
        res[name] = rep
    r = sum(t["R"] for t in trades)
    tst = [t["R"] for t in trades if t["epoch"] >= test_cut]
    print(f"  {label:52} n={len(trades):4} R={r:+7.1f} | testR={sum(tst):+6.1f} "
          f"| pass tr/te/full = "
          f"{res['train']['pass_rate'] if res['train'] else 0:3.0f}%/"
          f"{res['test']['pass_rate'] if res['test'] else 0:3.0f}%/"
          f"{res['full']['pass_rate'] if res['full'] else 0:3.0f}% "
          f"| te_days={res['test']['med_days'] if res['test'] else '—'} "
          f"| te_resolved={res['test']['resolved'] if res['test'] else 0}")
    return res


def short(f):
    return ",".join(f"{k}={v}" for k, v in f.items() if v not in ("all", "both", "any"))


def mode_fcfs():
    print("=== FCFS 1-TRADE-AT-A-TIME REALISM (vs independent overlapping) ===")
    for chan, variant, f, risk, thr in TOP:
        rows = load_rows(chan, variant)
        filt, _, _ = apply_filters(rows, f)
        print(f"\n[{chan} {variant} | {short(f)} | risk={risk} consec{thr}]")
        eval_windows(filt, risk, thr, "independent (deep-eval assumption)")
        fc, dropped = fcfs_filter(filt)
        eval_windows(fc, risk, thr, f"FCFS 1-at-a-time (dropped {dropped})")


def mode_stress():
    print("=== COST/SLIPPAGE STRESS: 4p cost + 0.5p slip (vs 3p/0) ===")
    need = sorted({(c, v) for c, v, *_ in TOP for c in
                   (("gary", "gsn") if c == "merged" else (c,))})
    for chan, variant in need:
        p = TRADES / f"{chan}__{variant}__c4_s0.5.jsonl"
        if not p.exists():
            subprocess.run([sys.executable, "research/precompute_trades.py",
                            chan, variant, "4", "0.5"], cwd=SL.REPO, check=True,
                           capture_output=True)
    for chan, variant, f, risk, thr in TOP:
        rows3 = load_rows(chan, variant, "c3")
        rows4 = load_rows(chan, variant, "c4_s0.5")
        print(f"\n[{chan} {variant} | {short(f)} | risk={risk} consec{thr}]")
        fa, _, _ = apply_filters(rows3, f)
        fa, _ = fcfs_filter(fa)
        eval_windows(fa, risk, thr, "3p cost, 0 slip (FCFS)")
        fb, _, _ = apply_filters(rows4, f)
        fb, _ = fcfs_filter(fb)
        eval_windows(fb, risk, thr, "4p cost, 0.5p slip (FCFS)")


def mode_regime3y():
    print("=== 3-YEAR REGIME RED-FLAG (same filters applied 2023-2026) ===")
    need = sorted({(c, v) for c, v, *_ in TOP for c in
                   (("gary", "gsn") if c == "merged" else (c,))})
    dest_tag = "c3_full3y"
    env = {**os.environ, "SWEEP_DATA_START": "2023-06-01",
           "SWEEP_OUT_TAG": dest_tag}
    for chan, variant in need:
        p = TRADES / f"{chan}__{variant}__{dest_tag}.jsonl"
        if not p.exists():
            subprocess.run([sys.executable, "research/precompute_trades.py",
                            chan, variant, "3"], cwd=SL.REPO, env=env,
                           check=True, capture_output=True, text=True)
    for chan, variant, f, risk, thr in TOP:
        rows = load_rows(chan, variant, dest_tag)
        filt, _, _ = apply_filters(rows, f)
        filt, _ = fcfs_filter(filt)
        print(f"\n[{chan} {variant} | {short(f)} | FCFS]")
        byy = {}
        for t in filt:
            y = datetime.fromtimestamp(t["epoch"], tz=timezone.utc).year
            byy.setdefault(y, []).append(t["R"])
        for y in sorted(byy):
            g = byy[y]; w = sum(1 for x in g if x > 0)
            print(f"    {y}: n={len(g):4} win={w/len(g)*100:4.0f}% R={sum(g):+8.1f}")


def mode_neighbors():
    print("=== NEIGHBOR STABILITY (toggle one filter dim; OOS test pass) ===")
    for chan, variant, f, risk, thr in TOP:
        rows = load_rows(chan, variant)
        print(f"\n[{chan} {variant} | {short(f)} | risk={risk} consec{thr}]")
        base, _, _ = apply_filters(rows, f)
        base, _ = fcfs_filter(base)
        eval_windows(base, risk, thr, "BASE")
        dims = {"session": list(SL.SESSIONS), "side": SL.SIDES, "t4": SL.T4,
                "t24": SL.T24, "addon": SL.ADDON, "slb": SL.SLB, "vol": SL.VOL}
        for dim, values in dims.items():
            for v in values:
                if v == f[dim]:
                    continue
                nf = {**f, dim: v}
                sub, _, _ = apply_filters(rows, nf)
                sub, _ = fcfs_filter(sub)
                if len(sub) < 40:
                    continue
                eval_windows(sub, risk, thr, f"  ~{dim}->{v}")


if __name__ == "__main__":
    mode = sys.argv[1]
    {"fcfs": mode_fcfs, "stress": mode_stress,
     "regime3y": mode_regime3y, "neighbors": mode_neighbors}[mode]()
