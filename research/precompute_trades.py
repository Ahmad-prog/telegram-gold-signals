"""
Precompute the per-trade stream for one (channel, variant) over the 3-year data.

    python3 research/precompute_trades.py <channel> <variant> [cost]

Writes research/trades/<channel>__<variant>__c<cost>.jsonl — one line per
simulated trade: epoch, R, outcome + entry-time features for filtering.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep_lib as SL

sys.path.insert(0, str(SL.REPO))
from engine import simulate

PIP = 0.10


def main():
    channel = sys.argv[1]
    variant = sys.argv[2]
    cost = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    slip = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0

    ep, o, h, l, c = SL.load_arrays()
    candles = SL.arrays_to_candles(ep, o, h, l, c)
    cfg = SL.variant_cfg(variant, cost=cost)
    cfg["market"]["slippage_pips"] = slip
    sigs = SL.prep_signal_rows(channel, ep, o, h, l, c)

    out = []
    for s in sigs:
        r = simulate(s, candles, s["_idx"], cfg)
        if r is None:
            continue
        entry = r["entry"]
        out.append({
            "epoch": ep[s["_idx"]],
            "exit_epoch": (datetime.fromisoformat(r["exit_date"]).timestamp()
                           if r.get("exit_date") else None),
            "date": s["date"],
            "channel": channel,
            "R": r["R"],
            "outcome": r["outcome"],
            "side": s["side"],
            "addon": bool(s.get("addon")),
            "sl_pips": abs(entry - s["sl"]) / PIP,
            "hour": s["hour"],
            "dow": s["dow"],
            "ret4h": s["ret4h"],
            "ret24h": s["ret24h"],
            "atr60": s["atr60"],
        })

    import os
    tag = os.environ.get("SWEEP_OUT_TAG") or (f"c{cost}" + (f"_s{slip}" if slip else ""))
    dest = SL.REPO / "research" / "trades" / f"{channel}__{variant}__{tag}.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    totR = sum(r["R"] for r in out)
    wins = sum(1 for r in out if r["R"] > 0)
    print(f"OK {channel} {variant} c{cost}: n={len(out)} win={wins/max(1,len(out))*100:.1f}% "
          f"R={totR:+.1f} -> {dest.name}")


if __name__ == "__main__":
    main()
