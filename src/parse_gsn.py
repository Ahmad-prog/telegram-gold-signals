"""
Parse GoldScalperNinja raw messages -> structured signals (Gary-compatible schema).

Format observed:
    💰 XAUUSD BUY 4039 - 4033       <- side + entry (single or range)
    🚨 SL : 4028                    <- stop (absolute)
    💵 TP : 4044, 4049, 4059        <- 3 absolute-price targets

Profit/management posts ("+140PIPS RUNNING PROFIT - TP HIT") and "Daily
Analysis" posts are ignored. Output schema matches data/signals.jsonl so the
existing engine/backtest run unchanged, plus a "channel" tag.
"""

import json
import re
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
RAW = DATA / "raw_goldscalperninja.jsonl"
OUT = DATA / "signals_goldscalperninja.jsonl"
UNPARSED = DATA / "unparsed_goldscalperninja.jsonl"
PIP = 0.10
CHANNEL = "goldscalperninja"

# Entry line: XAUUSD/GOLD + BUY/SELL + price (+ optional "- price2")
ENTRY_RE = re.compile(
    r"(?:xauusd|gold)\s+(buy|sell)\s+([0-9]{3,5}(?:\.[0-9]+)?)"
    r"(?:\s*[-/]\s*([0-9]{3,5}(?:\.[0-9]+)?))?",
    re.I,
)
SL_RE = re.compile(r"\bsl\b\s*[:\-]?\s*([0-9]{3,5}(?:\.[0-9]+)?)", re.I)
# Handle "TP : 4046, 4041, 4031" AND numbered "Tp1: .. Tp2: .. Tp3: .."
TP_ANY_RE = re.compile(
    r"\btp\s*\d?\s*[:\-]?\s*"
    r"((?:[0-9]{2,5}(?:\.[0-9]+)?)(?:\s*[,/]\s*[0-9]{2,5}(?:\.[0-9]+)?)*)",
    re.I,
)
TP_NUM_RE = re.compile(r"([0-9]{2,5}(?:\.[0-9]+)?)")
ADDON_RE = re.compile(r"\b(again|re-?entry|add|round\s*[2-9])\b", re.I)


def parse(text):
    em = ENTRY_RE.search(text)
    sm = SL_RE.search(text)
    if not em or not sm:
        return None
    side = em.group(1).lower()
    e1 = float(em.group(2))
    e2 = float(em.group(3)) if em.group(3) else None
    entry_low, entry_high = (min(e1, e2), max(e1, e2)) if e2 else (e1, e1)
    entry_mid = round((entry_low + entry_high) / 2, 3)
    sl = float(sm.group(1))

    tp_mode, tp_raw, tp_prices = None, [], []
    nums = []
    for m in TP_ANY_RE.finditer(text):
        for x in TP_NUM_RE.findall(m.group(1)):
            nums.append(float(x))
    seen = set()
    nums = [n for n in nums if n > 1000 and not (n in seen or seen.add(n))]
    # SANITY: drop typo'd targets >10% from entry (e.g. 19333 for 1933)
    nums = [n for n in nums if abs(n - entry_mid) <= 0.10 * entry_mid]
    if nums:
        tp_mode, tp_raw, tp_prices = "price", nums, nums
    return {
        "date": None, "side": side, "addon": bool(ADDON_RE.search(text)),
        "slowly": False, "entry_low": entry_low, "entry_high": entry_high,
        "entry_mid": entry_mid, "sl": sl, "tp_mode": tp_mode,
        "tp_raw": tp_raw, "tp_prices": tp_prices, "channel": CHANNEL,
        "raw": text.strip(),
    }


def looks_like_signal(text):
    return bool(ENTRY_RE.search(text)) and bool(re.search(r"\bsl\b", text, re.I))


def main():
    rows = [json.loads(l) for l in RAW.open(encoding="utf-8")]
    parsed, unparsed = [], []
    for r in rows:
        text = r["text"]
        if not looks_like_signal(text):
            continue
        sig = parse(text)
        if sig and sig["entry_low"] and (sig["sl"] is not None or sig["tp_mode"]):
            sig["date"] = r["date"]; sig["msg_id"] = r["id"]
            parsed.append(sig)
        else:
            unparsed.append({"date": r["date"], "id": r["id"], "text": text.strip()})

    with OUT.open("w", encoding="utf-8") as f:
        for s in parsed:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    with UNPARSED.open("w", encoding="utf-8") as f:
        for s in unparsed:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    buys = sum(1 for s in parsed if s["side"] == "buy")
    sells = sum(1 for s in parsed if s["side"] == "sell")
    no_sl = sum(1 for s in parsed if s["sl"] is None)
    no_tp = sum(1 for s in parsed if not s["tp_mode"])
    ntp = [len(s["tp_raw"]) for s in parsed if s["tp_raw"]]
    print(f"Parsed signals : {len(parsed)}  (buy {buys} / sell {sells})")
    print(f"  no SL value  : {no_sl}")
    print(f"  no TP parsed : {no_tp}")
    print(f"  avg #TPs     : {sum(ntp)/len(ntp):.1f}" if ntp else "  no TPs")
    print(f"  could NOT parse: {len(unparsed)} -> {UNPARSED.name}")
    print(f"\nSaved -> {OUT}")
    for s in parsed[:3]:
        print(json.dumps({k: s[k] for k in
              ("date", "side", "entry_low", "entry_high", "sl", "tp_mode", "tp_raw")},
              ensure_ascii=False))


if __name__ == "__main__":
    main()
