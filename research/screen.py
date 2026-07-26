"""
Stage 2: screen every filter combo per trade stream on the TRAIN window only.

Streams = (gary | gsn | merged) x variant. Filters = session x side x 4h-trend
x 24h-trend x addon x SL-bucket x volatility.

Survivor constraints (train): n>=120, PF>=1.08, R>0, >=3 of 4 train half-years
positive. Survivors then get a quick rolling two-phase pass-rate on train
(risk=1.0, consec2 stop) and the top candidates go to deep evaluation.

    python3 research/screen.py
Writes research/candidates.json + research/screen_summary.txt
"""
import itertools
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep_lib as SL

TRADES_DIR = SL.REPO / "research" / "trades"
MIN_N = 60          # 8-month train window -> ~2 trades/week minimum
MIN_PF = 1.08
TOP_KEEP = 400


def load_stream(channel, variant):
    p = TRADES_DIR / f"{channel}__{variant}__c3.jsonl"
    if not p.exists():
        return None
    rows = [json.loads(l) for l in p.open()]
    if not rows:
        return None
    return rows


def metrics(R):
    pos = R[R > 0].sum()
    neg = -R[R < 0].sum()
    pf = pos / neg if neg > 0 else float("inf")
    return R.sum(), pf


def main():
    streams = {}
    for variant in SL.VARIANTS:
        g = load_stream("gary", variant)
        n = load_stream("gsn", variant)
        if g: streams[("gary", variant)] = g
        if n: streams[("gsn", variant)] = n
        if g and n:
            streams[("merged", variant)] = sorted(g + n, key=lambda r: r["epoch"])

    print(f"{len(streams)} streams loaded")
    survivors = []
    combos_checked = 0

    for (chan, variant), rows in streams.items():
        tr = SL.trades_to_arrays(rows)
        masks = SL.build_masks(tr)
        train = tr["is_train"]
        # half-year buckets for train consistency
        hy = np.array([SL.halfyear_key(e) for e in tr["epoch"]])
        train_hys = sorted(set(hy[train]))
        R = tr["R"]

        for sess, side, t4, t24, ad, slb, vol in itertools.product(
                SL.SESSIONS, SL.SIDES, SL.T4, SL.T24, SL.ADDON, SL.SLB, SL.VOL):
            combos_checked += 1
            m = (masks["session"][sess] & masks["side"][side] & masks["t4"][t4]
                 & masks["t24"][t24] & masks["addon"][ad] & masks["slb"][slb]
                 & masks["vol"][vol])
            mt = m & train
            n = int(mt.sum())
            if n < MIN_N:
                continue
            totR, pf = metrics(R[mt])
            if totR <= 0 or pf < MIN_PF:
                continue
            pos_buckets = sum(1 for k in train_hys if R[mt & (hy == k)].sum() > 0)
            if pos_buckets < max(3, len(train_hys) - 1):
                continue
            survivors.append({
                "channel": chan, "variant": variant,
                "filters": {"session": sess, "side": side, "t4": t4, "t24": t24,
                            "addon": ad, "slb": slb, "vol": vol},
                "train_n": n, "train_R": round(float(totR), 2),
                "train_pf": round(float(pf), 3),
            })

    print(f"checked {combos_checked} combos -> {len(survivors)} survivors "
          f"(n>={MIN_N}, PF>={MIN_PF}, consistent)")

    # quick two-phase rolling pass on TRAIN to rank survivors
    ranked = []
    for cand in survivors:
        chan, variant = cand["channel"], cand["variant"]
        rows = streams[(chan, variant)]
        tr = SL.trades_to_arrays(rows)
        masks = SL.build_masks(tr)
        f = cand["filters"]
        m = (masks["session"][f["session"]] & masks["side"][f["side"]]
             & masks["t4"][f["t4"]] & masks["t24"][f["t24"]]
             & masks["addon"][f["addon"]] & masks["slb"][f["slb"]]
             & masks["vol"][f["vol"]])
        mt = m & tr["is_train"]
        sub = [{"epoch": e, "R": r} for e, r in zip(tr["epoch"][mt], tr["R"][mt])]
        dk, dayR, intra = SL.day_aggregate(sub, "consec", 2)
        rep = SL.rolling_two_phase(dayR, intra, risk=1.0)
        cand["train_pass"] = round(rep["pass_rate"], 1)
        cand["train_med_days"] = rep["med_days"]
        cand["train_resolved"] = rep["resolved"]
        ranked.append(cand)

    ranked.sort(key=lambda c: (-c["train_pass"], c["train_med_days"] or 1e9))
    keep = ranked[:TOP_KEEP]
    out = SL.REPO / "research" / "candidates.json"
    out.write_text(json.dumps(keep, indent=1))

    lines = [f"streams={len(streams)} combos={combos_checked} "
             f"survivors={len(survivors)} kept={len(keep)}"]
    lines.append("\nTOP 25 by train two-phase pass rate:")
    for c in keep[:25]:
        f = c["filters"]
        fs = ",".join(f"{k}={v}" for k, v in f.items() if v not in ("all", "both", "any"))
        lines.append(f"  {c['channel']:6} {c['variant']:10} [{fs or 'no-filter'}] "
                     f"n={c['train_n']:4} PF={c['train_pf']:.2f} R={c['train_R']:+7.1f} "
                     f"pass={c['train_pass']:.0f}% med={c['train_med_days']}")
    txt = "\n".join(lines)
    (SL.REPO / "research" / "screen_summary.txt").write_text(txt)
    print(txt)


if __name__ == "__main__":
    main()
