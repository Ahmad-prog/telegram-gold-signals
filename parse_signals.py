"""
STEP 2: parse raw GARY GOLD TRADER messages into structured, clean signals.

Reads  data/raw_messages.jsonl  (from fetch_history.py)
Writes data/signals.jsonl       (one structured entry signal per line)
       data/unparsed.jsonl       (signal-looking msgs we could NOT fully parse,
                                   so nothing is silently dropped)

Signal format observed (case-insensitive, punctuation varies):
    Gold Buy Now @ 4039 - 4032        <- side + entry (range or single)
    Sl: 4029                          <- stop loss (absolute) or "in LiveTrade"
    TP: 50/100Pips   OR   TP: 4320 / 4330   <- targets in PIPS or absolute price

Modifiers: "Again" (re-entry), "slowly". Management/analysis msgs are ignored.
"""

import json
import re
from pathlib import Path

DATA = Path(__file__).parent / "data"
RAW = DATA / "raw_messages.jsonl"
OUT = DATA / "signals.jsonl"
UNPARSED = DATA / "unparsed.jsonl"

# Gold pip convention: 1 pip = $0.10 price move (so 100 pips = $10).
# Change here if your broker counts gold pips differently.
PIP = 0.10

# A message is a candidate signal if it names Gold + Buy/Sell and has an SL line.
SIDE_RE = re.compile(r"gold\s+(buy|sell)\b", re.I)
# Re-entry / add-on markers Gary uses: "again", "agan" (typo), "more".
ADDON_RE = re.compile(r"\b(again|agan|more)\b", re.I)
SLOWLY_RE = re.compile(r"\bslow(?:ly)?\b", re.I)

# Entry: side, then ANY filler words (high/low/more/again/now/slowly...) on the
# same line, optional "@", then 1-2 gold-range numbers (4-5 digits).
ENTRY_RE = re.compile(
    r"gold\s+(?:buy|sell)[^\n]*?@?\s*\.?\s*"
    r"([0-9]{4,5}(?:\.[0-9]+)?)(?:\s*-\s*([0-9]{4,5}(?:\.[0-9]+)?))?",
    re.I,
)
SL_RE = re.compile(r"\bsl\b\s*[:\-]?\s*([0-9]{4,5}(?:\.[0-9]+)?)", re.I)
# TP: handle BOTH "TP: 50/100" (one line, pips or prices) AND numbered labels
# "Tp1: 5150 / Tp2: 5144 / Tp3: ...". Match `tp`, optional rung digit, then the
# value(s) after it. (`TP: open` won't match — no number — so it stays untargeted.)
TP_ANY_RE = re.compile(
    r"\btp\s*\d?\s*[:\-]?\s*"
    r"((?:[0-9]{2,5}(?:\.[0-9]+)?)(?:\s*/\s*[0-9]{2,5}(?:\.[0-9]+)?)*)",
    re.I,
)
TP_NUM_RE = re.compile(r"([0-9]{2,5}(?:\.[0-9]+)?)")


def parse(text):
    m = SIDE_RE.search(text)
    if not m:
        return None
    side = m.group(1).lower()

    em = ENTRY_RE.search(text)
    if not em:
        return None
    e1 = float(em.group(1))
    e2 = float(em.group(2)) if em.group(2) else None
    if e2 is not None:
        entry_low, entry_high = min(e1, e2), max(e1, e2)
    else:
        entry_low = entry_high = e1
    entry_mid = round((entry_low + entry_high) / 2, 3)

    sm = SL_RE.search(text)
    sl = float(sm.group(1)) if sm else None  # None when "Sl: in LiveTrade"

    # Take-profits: figure out pips vs absolute prices by magnitude.
    tp_mode = None
    tp_raw = []
    tp_prices = []
    nums = []
    for m in TP_ANY_RE.finditer(text):
        for x in TP_NUM_RE.findall(m.group(1)):
            nums.append(float(x))
    seen = set()
    nums = [n for n in nums if not (n in seen or seen.add(n))]   # dedupe, keep order
    if True:
        if nums:
            if max(nums) < 1000:  # pip-based (e.g. 50/100)
                nums = [n for n in nums if n <= 1000]   # drop pip typos
                tp_mode = "pips"
                tp_raw = nums
                for p in nums:
                    if side == "buy":
                        tp_prices.append(round(entry_mid + p * PIP, 3))
                    else:
                        tp_prices.append(round(entry_mid - p * PIP, 3))
            else:  # absolute price targets (e.g. 4320 / 4330)
                # SANITY: drop typo'd targets absurdly far from entry (e.g. 19333
                # for 1933). Gold doesn't move >10% intraday — reject outliers.
                nums = [n for n in nums if abs(n - entry_mid) <= 0.10 * entry_mid]
                if nums:
                    tp_mode = "price"
                    tp_raw = nums
                    tp_prices = nums

    return {
        "date": None,  # filled by caller
        "side": side,
        "addon": bool(ADDON_RE.search(text)),  # re-entry / add-to-position
        "slowly": bool(SLOWLY_RE.search(text)),
        "entry_low": entry_low,
        "entry_high": entry_high,
        "entry_mid": entry_mid,
        "sl": sl,
        "tp_mode": tp_mode,
        "tp_raw": tp_raw,
        "tp_prices": tp_prices,
        "raw": text.strip(),
    }


def looks_like_signal(text):
    """Candidate = mentions Gold buy/sell AND has an SL marker."""
    return bool(SIDE_RE.search(text)) and bool(re.search(r"\bsl\b", text, re.I))


def main():
    rows = [json.loads(l) for l in RAW.open(encoding="utf-8")]
    parsed, unparsed = [], []

    for r in rows:
        text = r["text"]
        if not looks_like_signal(text):
            continue
        sig = parse(text)
        if sig and sig["entry_low"] and (sig["sl"] is not None or sig["tp_mode"]):
            sig["date"] = r["date"]
            sig["msg_id"] = r["id"]
            parsed.append(sig)
        else:
            unparsed.append({"date": r["date"], "id": r["id"], "text": text.strip()})

    with OUT.open("w", encoding="utf-8") as f:
        for s in parsed:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    with UNPARSED.open("w", encoding="utf-8") as f:
        for s in unparsed:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    # Summary
    buys = sum(1 for s in parsed if s["side"] == "buy")
    sells = sum(1 for s in parsed if s["side"] == "sell")
    pip_tp = sum(1 for s in parsed if s["tp_mode"] == "pips")
    price_tp = sum(1 for s in parsed if s["tp_mode"] == "price")
    no_sl = sum(1 for s in parsed if s["sl"] is None)
    reentry = sum(1 for s in parsed if s["addon"])

    print(f"Parsed signals : {len(parsed)}  (buy {buys} / sell {sells})")
    print(f"  pip-based TP  : {pip_tp}")
    print(f"  price TP      : {price_tp}")
    print(f"  re-entries    : {reentry}")
    print(f"  no SL value   : {no_sl}  (e.g. 'Sl: in LiveTrade')")
    print(f"  could NOT parse: {len(unparsed)}  -> data/unparsed.jsonl (review)")
    print(f"\nSaved -> {OUT}")
    print("\n--- 3 parsed examples ---")
    for s in parsed[:3]:
        print(json.dumps({k: s[k] for k in
              ("date", "side", "entry_low", "entry_high", "sl",
               "tp_mode", "tp_raw", "tp_prices")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
