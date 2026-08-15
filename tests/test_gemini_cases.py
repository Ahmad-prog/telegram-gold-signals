"""
Gemini classifier test cases — 15 message types with expected readings.

    python3 tests/test_gemini_cases.py --record   # hits the API once (~15 calls, ~$0.006)
    python3 tests/test_gemini_cases.py            # replays the cassette, FREE

--record saves every response to tests/gemini_cassette.json, so the assertions
can be re-run forever at zero cost. Message text is taken verbatim from the real
Gary / GoldScalperNinja channels.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

CASSETTE = ROOT / "tests" / "gemini_cassette.json"

# (id, message text, expectations)
#   kind      : required classification
#   side      : required side for entries
#   sl        : required stop (None = "must be absent/0")
#   addon     : required is_addon
#   action    : required action
#   tp_absent : the model must NOT return this value (typo guard)
CASES = [
 ("gsn_entry_price_tps",
  "📊😉😌😍🥰⚡️⚡️⚡️⚡️\n\n💰 XAUUSD SELL 4051 - 4057\n\n🚨 SL : 4062\n\n"
  "💵 TP : 4046, 4041, 4031\n\nRISK SMART, MANAGE RISK WISELY ‼️‼️",
  {"kind": "entry", "side": "sell", "sl": 4062.0, "addon": False}),

 ("gary_entry_pip_tps",
  "Gold Buy Now @ 4039 - 4032\n\nSl: 4029\n\nTP: 50/100Pips",
  {"kind": "entry", "side": "buy", "sl": 4029.0, "addon": False}),

 ("gary_entry_numbered_tps",
  "Gold Sell Now slowly @ 5154 - 5158\n\nSl: 5161\n\nTp1: 5150\nTp2: 5144",
  {"kind": "entry", "side": "sell", "sl": 5161.0, "addon": False}),

 ("gsn_profit_recap",
  "Woohooo, Sell Gold Waterfall 🥂\n\n+140PIPS RUNNING PROFIT - TP HIT ✅✅\n\n"
  "Secure almost profit & set breakeven for zero risk if you want.",
  {"kind": "noise"}),

 ("gsn_running_profit",
  "Sell Gold on TOP 🤑\n\n~70PIPS RUNNING PROFIT - TP HIT ✅\n\n"
  "Take profit & set breakeven for zero risk if you want",
  {"kind": "noise"}),

 ("gsn_daily_analysis",
  "1️⃣2️⃣3️⃣4️⃣ XAUUSD — Daily Analysis 🕯\n\n🗓 29 June | M30 Timeframe\n\n"
  "🔤🔤 SELL Zone — marked in chart\n🔤🔤 BUY Zone — marked in chart\n\n"
  "This is a personal analysis based on my own experience. Trade at your own risk.",
  {"kind": "noise"}),

 ("gary_no_numeric_sl",
  "Gold Buy Now @ 4119 - 4114\n\nSl: in LiveTrade\nTP: in LiveTrade",
  {"kind": "entry", "side": "buy", "sl": None}),

 ("update_move_to_be",
  "Move your SL to breakeven on the gold sell, secure the position now",
  {"kind": "update", "action": "modify_sl"}),

 ("update_close_half",
  "Round 1,2 Sell — take partial profit now, close half and hold the rest",
  {"kind": "update", "action": "close_partial"}),

 ("update_close_all",
  "Close all gold positions now, market getting choppy ahead of the news",
  {"kind": "update", "action": "close_all"}),

 ("update_sl_correction",
  "Correction on the last signal — SL should be 4029 not 4092, please adjust",
  {"kind": "update", "action": "modify_sl", "new_sl": 4029.0}),

 ("gary_addon_reentry",
  "Gold Sell Again @ 4055 - 4060\n\nSl: 4066\n\nTP: 50/100Pips",
  {"kind": "entry", "side": "sell", "sl": 4066.0, "addon": True}),

 ("typo_tp_19333",
  "GOLD SELL 1943 - 1947\n\nSL : 1948\n\nTP1 : 1941\nTP2 : 1939\nTP3 : 19333",
  {"kind": "entry", "side": "sell", "sl": 1948.0, "tp_absent": 19333.0}),

 ("promo_marketing",
  "🔥 Join our VIP channel for premium signals! Limited slots left.\n"
  "DM @admin to get access. Trade with me: 👉Vantage 👉Exness",
  {"kind": "noise"}),

 ("weekly_results_recap",
  "📊 Full Summary for Today\n\n✅ Gold Sell: +300 pips\n✅ Gold Buy: +150 pips\n"
  "✅ Gold Buy: +70 pips (Live Trade)\n✅ Gold Sell: +90 pips",
  {"kind": "noise"}),
]


def record():
    from gemini_classifier import classify, _client
    client = _client()
    out = {}
    print(f"Recording {len(CASES)} cases (~${len(CASES)*0.0004:.3f})...\n")
    for cid, text, _ in CASES:
        sig = classify(text, client)
        if sig is None:
            print(f"  {cid}: FAILED to classify"); continue
        out[cid] = sig.model_dump()
        print(f"  {cid:26} -> {sig.kind:6} {sig.side:5} action={sig.action}")
    CASSETTE.write_text(json.dumps(out, indent=1))
    print(f"\nsaved -> {CASSETTE.name}")


def check(cid, text, exp, got) -> tuple[bool, str]:
    if got is None:
        return False, "no recorded response"
    if got["kind"] != exp["kind"]:
        return False, f"kind={got['kind']} expected {exp['kind']}"
    if "side" in exp and got["side"] != exp["side"]:
        return False, f"side={got['side']} expected {exp['side']}"
    if "sl" in exp:
        want = exp["sl"]
        have = got["sl"] or None
        if want is None and have is not None:
            return False, f"invented SL {have} (post states none)"
        if want is not None and have != want:
            return False, f"sl={have} expected {want}"
    if "addon" in exp and bool(got["is_addon"]) != exp["addon"]:
        return False, f"is_addon={got['is_addon']} expected {exp['addon']}"
    if "action" in exp and got["action"] != exp["action"]:
        return False, f"action={got['action']} expected {exp['action']}"
    if "new_sl" in exp and got.get("new_sl") != exp["new_sl"]:
        return False, f"new_sl={got.get('new_sl')} expected {exp['new_sl']}"
    if "tp_absent" in exp and exp["tp_absent"] in (got.get("tps") or []):
        return False, f"passed through the typo value {exp['tp_absent']}"
    # universal: no number may be invented
    import re
    present = {f"{float(m):g}" for m in re.findall(r"\d+(?:\.\d+)?", text)}
    for v in ([got["sl"]] if got["sl"] else []) + list(got.get("tps") or []):
        if f"{float(v):g}" not in present:
            return False, f"hallucinated {v:g} (not in the post)"
    return True, "ok"


def replay():
    if not CASSETTE.exists():
        print("No cassette. Run with --record first (costs ~$0.006)."); sys.exit(1)
    rec = json.loads(CASSETTE.read_text())
    passed = failed = 0
    for cid, text, exp in CASES:
        got = rec.get(cid)
        ok, why = check(cid, text, exp, got)
        first = text.strip().splitlines()[0][:52]
        print(f"[{'PASS' if ok else 'FAIL'}] {cid:26} {got['kind'] if got else '-':6} | \"{first}\"")
        if not ok:
            print(f"       -> {why}")
        passed += ok; failed += (not ok)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    record() if "--record" in sys.argv else replay()
