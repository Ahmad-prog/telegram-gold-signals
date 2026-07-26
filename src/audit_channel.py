"""
AUDIT a candidate signal channel end-to-end: raw JSONL -> universal parse ->
corrected-engine backtest (Strategy A, 3p cost) -> hiring scorecard.

    python3 src/audit_channel.py data/raw_cand_x.jsonl [label]

Hiring bar (per side): exp >= +0.05R/trade @3p, win% >= breakeven+4pts,
>=100 signals/yr, >=8/12 months positive.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from engine import simulate, load_config

PIP = 0.10

SIDE_RE = re.compile(r"\b(?:xau\s*usd|xauusd|gold)\b.{0,60}?\b(buy|sell|long|short)\b|"
                     r"\b(buy|sell|long|short)\b.{0,60}?\b(?:xau\s*usd|xauusd|gold)\b",
                     re.I | re.S)
SL_RE = re.compile(r"(?:\bsl\b|s\.l\.?|stop\s*loss|stoploss)\s*[:\-@]?\s*"
                   r"(\d{3,5}(?:\.\d+)?)", re.I)
TP_RE = re.compile(r"(?:\btp\s*\d?\b|t\.p\.?\s*\d?|take\s*profit\s*(?:level)?\s*\d?|target\s*(?:level)?\s*\d?)"
                   r"\s*[:\-@]?\s*((?:\d{2,5}(?:\.\d+)?)(?:\s*[,/]\s*\d{2,5}(?:\.\d+)?)*)",
                   re.I)
NUM_RE = re.compile(r"(\d{2,5}(?:\.\d+)?)")
ENTRY_RE = re.compile(r"(?:buy|sell)[^\n]{0,30}?(\d{4}(?:\.\d+)?)"
                      r"(?:\s*[-/]\s*(\d{4}(?:\.\d+)?))?", re.I)
ADDON_RE = re.compile(r"\b(again|agan|more|re-?entry|add|round\s*[2-9])\b", re.I)


def parse(text):
    text = re.sub(r"(?<=\d),(?=\d)", "", text)   # 4,057.79 -> 4057.79
    m = SIDE_RE.search(text)
    if not m:
        return None
    side = (m.group(1) or m.group(2)).lower()
    side = {"long": "buy", "short": "sell"}.get(side, side)
    sm = SL_RE.search(text)
    if not sm:
        return None
    sl = float(sm.group(1))
    em = ENTRY_RE.search(text)
    if em:
        e1 = float(em.group(1))
        e2 = float(em.group(2)) if em.group(2) else None
        lo, hi = (min(e1, e2), max(e1, e2)) if e2 else (e1, e1)
    else:
        lo = hi = sl  # market post w/o entry price; zone unused by market_next
    mid = (lo + hi) / 2
    nums, seen = [], set()
    for tm in TP_RE.finditer(text):
        for x in NUM_RE.findall(tm.group(1)):
            v = float(x)
            if v not in seen:
                seen.add(v); nums.append(v)
    tp_mode, tp_raw, tp_prices = None, [], []
    if nums:
        if max(nums) < 1000:
            tp_mode, tp_raw = "pips", [n for n in nums if n <= 1000]
            tp_prices = [round(mid + p * PIP, 3) if side == "buy"
                         else round(mid - p * PIP, 3) for p in tp_raw]
        else:
            good = [n for n in nums if n > 1000 and abs(n - mid) <= 0.10 * mid]
            if good:
                tp_mode, tp_raw, tp_prices = "price", good, good
    return {"side": side, "sl": sl, "entry_low": lo, "entry_high": hi,
            "entry_mid": mid, "tp_mode": tp_mode, "tp_raw": tp_raw,
            "tp_prices": tp_prices, "addon": bool(ADDON_RE.search(text))}


def load_candles():
    import csv
    rows = {}
    for name in ("xauusd_1m_3y.csv", "xauusd_1m_recent.csv"):
        p = ROOT / "data" / name
        if not p.exists():
            continue
        with p.open() as f:
            for r in csv.DictReader(f):
                dt = datetime.fromisoformat(r["datetime_utc"]).replace(tzinfo=timezone.utc)
                rows[dt] = (dt, float(r["open"]), float(r["high"]),
                            float(r["low"]), float(r["close"]))
    return [rows[k] for k in sorted(rows)]


def main():
    raw_path = Path(sys.argv[1])
    label = sys.argv[2] if len(sys.argv) > 2 else raw_path.stem
    msgs = [json.loads(l) for l in raw_path.open(encoding="utf-8")]
    msgs.sort(key=lambda r: r["date"])

    cfg = load_config()
    cfg["market"]["round_trip_cost_pips"] = 3
    cfg["market"]["slippage_pips"] = 0.0
    cfg["entry"]["mode"] = "market_next"
    cfg["exit"]["strategy"] = "A"
    candles = load_candles()
    times = [c[0] for c in candles]
    import bisect
    from datetime import timedelta

    parsed = 0
    trades = []
    for r in msgs:
        s = parse(r.get("text") or "")
        if not s or not s["tp_mode"]:
            continue
        parsed += 1
        s["date"] = r["date"]
        t = datetime.fromisoformat(r["date"])
        i = bisect.bisect_right(times, t)
        if i >= len(candles) or (candles[i][0] - t) > timedelta(days=3):
            continue
        res = simulate(s, candles, i, cfg)
        if res is None:
            continue
        trades.append({**res, "side": s["side"], "addon": s["addon"],
                       "month": r["date"][:7]})

    print(f"=== AUDIT: {label} ===")
    print(f"messages {len(msgs)} | parsed signals {parsed} | simulated trades {len(trades)}")
    if not trades:
        print("VERDICT: NOT AUDITABLE (no parseable+simulatable signals)")
        return

    def scorecard(g, name):
        n = len(g)
        if n < 20:
            print(f"  {name:12} n={n:4}  (too few to judge)")
            return False
        w = [t["R"] for t in g if t["R"] > 0]
        l = [t["R"] for t in g if t["R"] <= 0]
        aw = sum(w) / len(w) if w else 0
        al = sum(l) / len(l) if l else -1
        win = len(w) / n * 100
        be = abs(al) / (abs(al) + aw) * 100 if aw > 0 else 100
        exp = sum(t["R"] for t in g) / n
        bym = defaultdict(float)
        for t in g:
            bym[t["month"]] += t["R"]
        pos = sum(1 for v in bym.values() if v > 0)
        months = len(bym)
        ok = exp >= 0.05 and (win - be) >= 4 and n >= 80 and pos >= months * 0.6
        print(f"  {name:12} n={n:4} win={win:5.1f}% (breakeven {be:.1f}%, edge {win-be:+.1f}pts) "
              f"netR={sum(t['R'] for t in g):+7.1f} exp={exp:+.3f}R "
              f"posMonths {pos}/{months}  {'✅ HIRE' if ok else '❌'}")
        return ok

    print(f"{'-'*76}")
    any_hire = False
    for side in ("sell", "buy"):
        any_hire |= scorecard([t for t in trades if t["side"] == side], f"{side}s")
    scorecard(trades, "ALL")
    fresh = [t for t in trades if not t["addon"]]
    if len(fresh) != len(trades):
        for side in ("sell", "buy"):
            any_hire |= scorecard([t for t in fresh if t["side"] == side],
                                  f"{side}s-fresh")
    print(f"VERDICT: {'✅ candidate has a hireable slice' if any_hire else '❌ no hireable slice'}")


if __name__ == "__main__":
    main()
