"""
Guardrail tests for the LLM parsing layer — NO API CALLS.

Feeds hand-built LLMSignal objects (including deliberately malicious/hallucinated
ones) through validate_extraction() and arbitrate(), proving the safety net holds
before any real money or API key is involved.

    python3 tests/test_llm_guardrails.py     -> "N passed, 0 failed"
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llm_parser import LLMSignal, validate_extraction, arbitrate

CFG = {"live": {"sl_pips_min": 40, "sl_pips_max": 120, "max_price_drift_pct": 1.0}}

GOOD_RAW = "XAUUSD SELL 4051 - 4057\nSL : 4062\nTP : 4046, 4041, 4031"


def sig(**kw):
    base = dict(kind="entry", side="sell", entry_low=4051.0, entry_high=4057.0,
                sl=4062.0, tps=[4046.0, 4041.0, 4031.0], tp_unit="price",
                is_addon=False, action="open", confidence=0.95, reason="test")
    base.update(kw)
    return LLMSignal(**base)


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn)); return fn
    return deco


# ---------------------------------------------------------- validate_extraction

@case("valid sell signal passes")
def _():
    ok, why = validate_extraction(sig(), GOOD_RAW, CFG)
    return ok is True, why


@case("HALLUCINATED price is rejected")
def _():
    # 4099 appears nowhere in the source text
    ok, why = validate_extraction(sig(sl=4099.0), GOOD_RAW, CFG)
    return ok is False and "hallucinated" in why, why


@case("SL on wrong side of entry is rejected (sell)")
def _():
    raw = "XAUUSD SELL 4051 - 4057\nSL : 4040\nTP : 4046"
    ok, why = validate_extraction(sig(sl=4040.0, tps=[4046.0]), raw, CFG)
    return ok is False and "wrong side" in why, why


@case("SL on wrong side of entry is rejected (buy)")
def _():
    raw = "XAUUSD BUY 4051 - 4057\nSL : 4062\nTP : 4070"
    ok, why = validate_extraction(sig(side="buy", sl=4062.0, tps=[4070.0]), raw, CFG)
    return ok is False and "wrong side" in why, why


@case("TP on wrong side (the 19333-typo class) is rejected")
def _():
    raw = "GOLD SELL 1943 - 1947\nSL : 1948\nTP1 : 1941\nTP2 : 19333"
    ok, why = validate_extraction(
        sig(entry_low=1943.0, entry_high=1947.0, sl=1948.0, tps=[19333.0]), raw, CFG)
    return ok is False, why


@case("SL too tight is rejected (below band)")
def _():
    # mid 4054, SL 4056 -> 20 pips, under the 40p minimum
    raw = "XAUUSD SELL 4051 - 4057\nSL : 4056\nTP : 4046"
    ok, why = validate_extraction(sig(sl=4056.0, tps=[4046.0]), raw, CFG)
    return ok is False and "band" in why, why


@case("SL too wide is rejected (above band)")
def _():
    raw = "XAUUSD SELL 4051 - 4057\nSL : 4090\nTP : 4046"
    ok, why = validate_extraction(sig(sl=4090.0, tps=[4046.0]), raw, CFG)
    return ok is False and "band" in why, why


@case("stale signal far from live price is rejected")
def _():
    ok, why = validate_extraction(sig(), GOOD_RAW, CFG, live_price=3900.0)
    return ok is False and "live" in why, why


@case("entry with no numeric SL is rejected")
def _():
    raw = "Gold Buy Now @ 4119 - 4114\nSl: in LiveTrade\nTP: in LiveTrade"
    ok, why = validate_extraction(
        sig(side="buy", entry_low=4114.0, entry_high=4119.0, sl=None, tps=[]), raw, CFG)
    return ok is False and "stop-loss" in why, why


@case("noise post passes validation (nothing to trade)")
def _():
    ok, why = validate_extraction(
        sig(kind="noise", side=None, entry_low=None, entry_high=None, sl=None,
            tps=[], tp_unit=None, action="none"), "+140 PIPS TP HIT", CFG)
    return ok is True and "noise" in why, why


@case("pip-unit TPs validate on the correct side")
def _():
    raw = "Gold Buy Now @ 4039 - 4032\nSl: 4029\nTP: 50/100Pips"
    ok, why = validate_extraction(
        sig(side="buy", entry_low=4032.0, entry_high=4039.0, sl=4029.0,
            tps=[50.0, 100.0], tp_unit="pips"), raw, CFG)
    return ok is True, why


# ---------------------------------------------------------------- arbitrate

REGEX_SELL = {"side": "sell", "sl": 4062.0, "entry_low": 4051.0, "entry_high": 4057.0}


@case("arbiter: both agree -> take")
def _():
    v = arbitrate(REGEX_SELL, sig(), GOOD_RAW, CFG)
    return v["decision"] == "take" and v["source"] == "both_agree", v


@case("arbiter: DISAGREE on side -> skip")
def _():
    # LLM's buy reading is internally VALID (SL below entry, TP above), so it
    # reaches the side comparison rather than being caught by geometry first.
    raw = "GOLD 4051 - 4057\nSL 4046\nTP 4062"
    llm_buy = sig(side="buy", sl=4046.0, tps=[4062.0])
    v = arbitrate({"side": "sell", "sl": 4046.0}, llm_buy, raw, CFG)
    return v["decision"] == "skip" and v.get("conflict") is True, v


@case("arbiter: DISAGREE on SL -> skip")
def _():
    raw = GOOD_RAW + "\n4031"
    v = arbitrate({"side": "sell", "sl": 4031.0}, sig(), raw, CFG)
    return v["decision"] == "skip" and v.get("conflict") is True, v


@case("arbiter: regex missed it, llm clean -> take (llm_only)")
def _():
    v = arbitrate(None, sig(), GOOD_RAW, CFG)
    return v["decision"] == "take" and v["source"] == "llm_only", v


@case("arbiter: regex saw signal, llm says noise -> skip (conflict)")
def _():
    v = arbitrate(REGEX_SELL, sig(kind="noise", action="none"), GOOD_RAW, CFG)
    return v["decision"] == "skip" and v.get("conflict") is True, v


@case("arbiter: llm unavailable (None) -> fall back to regex")
def _():
    v = arbitrate(REGEX_SELL, None, GOOD_RAW, CFG)
    return v["decision"] == "take" and v["source"] == "regex_only", v


@case("arbiter: nothing from either -> skip")
def _():
    v = arbitrate(None, None, "gm traders", CFG)
    return v["decision"] == "skip", v


@case("arbiter: management post -> manage, never a new trade")
def _():
    v = arbitrate(None, sig(kind="management", action="move_sl_be"), "Move SL to BE", CFG)
    return v["decision"] == "manage" and v["action"] == "move_sl_be", v


@case("arbiter: llm-only entry that fails a guardrail -> skip")
def _():
    v = arbitrate(None, sig(sl=4099.0), GOOD_RAW, CFG)
    return v["decision"] == "skip", v


def main():
    passed = failed = 0
    for name, fn in CASES:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"EXCEPTION {type(e).__name__}: {e}"
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  -> {detail}"))
        passed += ok; failed += (not ok)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
