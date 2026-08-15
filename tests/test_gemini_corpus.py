"""
Gemini vs regex on REAL channel messages — the agreement check.

    python3 tests/test_gemini_corpus.py --record   # ~25 API calls (~$0.010)
    python3 tests/test_gemini_corpus.py            # replay, FREE

Samples a deterministic mix of real Gary / GoldScalperNinja posts (entries,
recaps, analysis), classifies each with Gemini, and diffs against the regex
parser. Disagreements are what matter: in the live pipeline they never trade.
"""
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import parse_gary, parse_gsn

CASSETTE = ROOT / "tests" / "gemini_corpus_cassette.json"
N_SIGNAL, N_NOISE = 16, 9        # 25 total


def sample():
    """Deterministic mix: signal-looking posts + noise-looking posts."""
    rng = random.Random(20260814)
    picked = []
    for fn, P, ch in (("data/raw_gary_max.jsonl", parse_gary, "gary"),
                      ("data/raw_gsn_max.jsonl", parse_gsn, "gsn")):
        rows = [json.loads(l) for l in (ROOT / fn).open(encoding="utf-8")
                if json.loads(l)["date"] >= "2025-07"]
        sig, noise = [], []
        for r in rows:
            t = r["text"]
            if not re.search(r"gold|xau", t, re.I):
                continue
            (sig if P.looks_like_signal(t) and P.parse(t) else noise).append(r)
        picked += [(ch, r) for r in rng.sample(sig, min(N_SIGNAL // 2, len(sig)))]
        picked += [(ch, r) for r in rng.sample(noise, min(N_NOISE // 2 + 1, len(noise)))]
    picked.sort(key=lambda x: x[1]["date"])
    return picked


def record():
    from gemini_classifier import classify, _client
    client = _client()
    rows = sample()
    print(f"Recording {len(rows)} real messages (~${len(rows)*0.0004:.3f})...\n")
    out = []
    for ch, r in rows:
        sig = classify(r["text"], client)
        if sig is None:
            continue
        P = parse_gary if ch == "gary" else parse_gsn
        rx = P.parse(r["text"]) if P.looks_like_signal(r["text"]) else None
        out.append({
            "channel": ch, "msg_id": r["id"], "date": r["date"],
            "text": r["text"],
            "gemini": sig.model_dump(),
            "regex": {"side": rx["side"], "sl": rx["sl"], "tp_mode": rx["tp_mode"]} if rx else None,
        })
        print(f"  {ch:4} {r['date'][:10]} -> {sig.kind:6} "
              f"regex={'signal' if rx else 'none  '}")
    CASSETTE.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"\nsaved {len(out)} -> {CASSETTE.name}")


def replay():
    if not CASSETTE.exists():
        print("No cassette. Run with --record first (~$0.010)."); sys.exit(1)
    rows = json.loads(CASSETTE.read_text())

    agree = conflict = gem_only = rx_only = both_noise = 0
    problems = []
    for r in rows:
        g, rx = r["gemini"], r["regex"]
        g_entry = g["kind"] == "entry"
        if g_entry and rx:
            if g["side"] == rx["side"] and (not rx["sl"] or not g["sl"]
                                            or abs(g["sl"] - rx["sl"]) < 0.01):
                agree += 1
            else:
                conflict += 1
                problems.append((r, f"side {g['side']}/{rx['side']} sl {g['sl']}/{rx['sl']}"))
        elif g_entry and not rx:
            gem_only += 1
        elif not g_entry and rx:
            rx_only += 1
            problems.append((r, f"regex saw a signal, gemini said {g['kind']}"))
        else:
            both_noise += 1

        # hard invariant: never invent a number
        present = {f"{float(m):g}" for m in re.findall(r"\d+(?:\.\d+)?", r["text"])}
        for v in ([g["sl"]] if g["sl"] else []) + list(g.get("tps") or []):
            if f"{float(v):g}" not in present:
                problems.append((r, f"HALLUCINATED {v:g}"))

    total = len(rows)
    print(f"{total} real messages classified\n")
    print(f"  both agree it's an entry     {agree:3}")
    print(f"  both agree it's not          {both_noise:3}")
    print(f"  gemini entry, regex none     {gem_only:3}   (regex gaps — LLM adds coverage)")
    print(f"  regex signal, gemini not     {rx_only:3}   (would SKIP in live pipeline)")
    print(f"  field-level conflicts        {conflict:3}   (would SKIP in live pipeline)")
    decided = agree + both_noise
    print(f"\n  agreement: {decided}/{total} = {decided/total*100:.0f}%")

    halluc = [p for p in problems if "HALLUCINATED" in p[1]]
    print(f"  hallucinated numbers: {len(halluc)}  (must be 0)")
    if problems:
        print("\n  --- cases the pipeline would flag ---")
        for r, why in problems[:8]:
            print(f"    {r['channel']} {r['date'][:10]} {why}")
            print(f"      \"{r['text'].strip().splitlines()[0][:64]}\"")

    # the only hard failure is a hallucination; disagreements are handled by design
    sys.exit(1 if halluc else 0)


if __name__ == "__main__":
    record() if "--record" in sys.argv else replay()
