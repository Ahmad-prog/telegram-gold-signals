"""
Stage 3: deep-evaluate screened candidates.

For each candidate: rolling-start TWO-PHASE (P1 +8% then P2 +6%) evaluation on
  - TRAIN window (2023-07 .. 2025-06)  — in-sample
  - TEST window  (2025-07 .. 2026-06)  — out-of-sample (selection-blind)
  - FULL 3 years
across risk in {0.5, 0.75, 1.0, 1.5}% and stop in {consec2, consec3, none}.

    python3 research/deep_eval.py --chunk 0 --nchunks 6
Writes research/deep/chunk_<i>.jsonl
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep_lib as SL

TRADES_DIR = SL.REPO / "research" / "trades"
RISKS = [0.5, 0.75, 1.0, 1.5]
STOPS = [("consec", 2), ("consec", 3), ("none", 0)]


def get_stream(cache, chan, variant):
    key = (chan, variant)
    if key in cache:
        return cache[key]
    if chan == "merged":
        rows = get_stream(cache, "gary", variant) + get_stream(cache, "gsn", variant)
        rows = sorted(rows, key=lambda r: r["epoch"])
    else:
        p = TRADES_DIR / f"{chan}__{variant}__c3.jsonl"
        rows = [json.loads(l) for l in p.open()]
    cache[key] = rows
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", type=int, default=0)
    ap.add_argument("--nchunks", type=int, default=1)
    args = ap.parse_args()

    cands = json.loads((SL.REPO / "research" / "candidates.json").read_text())
    mine = [c for i, c in enumerate(cands) if i % args.nchunks == args.chunk]
    print(f"chunk {args.chunk}/{args.nchunks}: {len(mine)} candidates")

    cache = {}
    results = []
    from datetime import datetime, timezone
    train_cut = datetime.fromisoformat(SL.TRAIN_END).replace(tzinfo=timezone.utc).timestamp()

    for c in mine:
        rows = get_stream(cache, c["channel"], c["variant"])
        tr = SL.trades_to_arrays(rows)
        masks = SL.build_masks(tr)
        f = c["filters"]
        m = (masks["session"][f["session"]] & masks["side"][f["side"]]
             & masks["t4"][f["t4"]] & masks["t24"][f["t24"]]
             & masks["addon"][f["addon"]] & masks["slb"][f["slb"]]
             & masks["vol"][f["vol"]])
        sub_all = [{"epoch": e, "R": r} for e, r in zip(tr["epoch"][m], tr["R"][m])]

        for stop_mode, thr in STOPS:
            dk, dayR, intra = SL.day_aggregate(sub_all, stop_mode, thr)
            if len(dayR) == 0:
                continue
            # day-level window masks
            dk_ep = dk * 86400.0 - (24 - SL.RESET_HOUR) * 3600  # approx day start epoch
            is_train_day = dk_ep < train_cut
            is_test_day = ~is_train_day
            for risk in RISKS:
                row = {**c, "stop": f"{stop_mode}{thr if stop_mode!='none' else ''}",
                       "risk": risk}
                for wname, wmask in (("train", is_train_day), ("test", is_test_day),
                                     ("full", np.ones_like(is_train_day, bool))):
                    idxs = np.where(wmask)[0]
                    if len(idxs) == 0:
                        row[f"{wname}_pass"] = None
                        continue
                    lo, hi = idxs[0], idxs[-1]
                    # evaluation runs forward from each start day within the
                    # window; it may extend beyond the window end only for
                    # 'full'/'train' (train evals may bleed into test period —
                    # prevent that by slicing hard).
                    dR, di = dayR[lo:hi + 1], intra[lo:hi + 1]
                    rep = SL.rolling_two_phase(dR, di, risk)
                    row[f"{wname}_pass"] = round(rep["pass_rate"], 1)
                    row[f"{wname}_resolved"] = rep["resolved"]
                    row[f"{wname}_med_days"] = rep["med_days"]
                results.append(row)

    dest = SL.REPO / "research" / "deep" / f"chunk_{args.chunk}.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w") as fo:
        for r in results:
            fo.write(json.dumps(r) + "\n")
    print(f"OK wrote {len(results)} rows -> {dest.name}")


if __name__ == "__main__":
    main()
