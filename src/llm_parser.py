"""
LLM parsing layer — Claude reads a raw Telegram post and returns a structured
signal, for the cases regex fundamentally cannot handle:

  * a NEW provider whose format we've never seen (no regex written yet)
  * FORMAT DRIFT on an existing provider (Gary switched to "Tp1:/Tp2:" mid-2026
    and the regex silently dropped 85% of his take-profits for weeks)
  * MANAGEMENT posts ("close half", "move SL to BE", "cancel that trade")
  * EDITS / amendments ("ignore the last one", "SL should be 4029 not 4092")

DESIGN RULE: the LLM never replaces the regex on the money path — it is a
second opinion. `parse_with_llm()` returns a decision, and `arbitrate()`
combines regex + LLM into one verdict:

    both agree            -> TAKE (highest confidence)
    regex only            -> TAKE (regex is deterministic and already validated)
    LLM only              -> TAKE only if it passes every guardrail
    they disagree         -> SKIP + alert  (never guess with money)

GUARDRAILS (all enforced in `validate_extraction`, no API needed to test):
  1. structured output — schema-validated, cannot return malformed JSON
  2. no invented numbers — every price the LLM emits must appear in the source
  3. geometry — SL/TP must sit on the profitable side of entry for the side
  4. sanity — SL distance inside the configured pip band, TPs within 10% of entry
  5. market proximity — entry within `max_price_drift_pct` of live price
  6. fail-safe — any exception, refusal, or timeout => decision "skip"

Requires ANTHROPIC_API_KEY (or an `ant auth login` profile). Never called on
the critical path without a regex result already in hand.
"""

from __future__ import annotations

import json
import os
import re
from typing import Literal

from pydantic import BaseModel, Field

MODEL = "claude-opus-5"
PIP = 0.10

# ----------------------------------------------------------------- schema


class LLMSignal(BaseModel):
    """What Claude returns for one Telegram post."""

    kind: Literal["entry", "management", "noise"] = Field(
        description="entry = a NEW tradeable order with a stop. "
        "management = instruction about an EXISTING trade (close/move SL/cancel). "
        "noise = analysis, results recap, marketing, greeting, or anything else."
    )
    side: Literal["buy", "sell"] | None = Field(
        description="Direction for an entry signal; null for management/noise."
    )
    entry_low: float | None = Field(description="Low end of the entry zone, or the single entry price.")
    entry_high: float | None = Field(description="High end of the entry zone, or the single entry price.")
    sl: float | None = Field(description="Stop-loss as an absolute price. Null if the post does not state a numeric stop.")
    tps: list[float] = Field(description="Take-profit values in the order written. Empty if none stated.")
    tp_unit: Literal["price", "pips"] | None = Field(
        description="'price' if TPs are absolute gold prices (e.g. 4046), 'pips' if distances (e.g. 50/100)."
    )
    is_addon: bool = Field(description="True if this adds to / re-enters an existing position ('again', 'more', 'round 2').")
    action: Literal["open", "close_partial", "close_all", "move_sl_be", "cancel", "none"] = Field(
        description="For management posts, what to do with the open trade. 'open' for entries, 'none' for noise."
    )
    confidence: float = Field(description="0.0-1.0 confidence that this reading is correct.")
    reason: str = Field(description="One short sentence explaining the classification.")


SYSTEM = """You extract XAUUSD (gold) trading signals from Telegram posts by retail signal providers.

You are one half of a two-source system: a regex parser reads the same post, and a
trade is only taken when both agree. Your job is accuracy, not coverage — a wrong
reading costs real money, an abstention costs nothing.

CLASSIFY every post as exactly one of:

- "entry": a NEW order the follower should place now. It names a direction and
  normally an entry price/zone and a numeric stop-loss.
- "management": an instruction about a trade ALREADY open — take partial profit,
  move stop to breakeven, close everything, cancel a pending order, or a correction
  to a previous post ("SL should be X not Y", "ignore the last signal").
- "noise": everything else. Results recaps ("+140 pips TP HIT ✅"), market analysis,
  chart commentary, education, promotions, greetings, VIP advertising.

CRITICAL RULES:

1. NEVER invent a number. Every price you output must appear verbatim in the post.
   If the post says "SL: in LiveTrade" or "TP: open", there is no numeric value —
   return null / an empty list. Do not infer, calculate, or round.
2. A post celebrating a result is NOT an entry, even though it names a direction.
   "Sell Gold on TOP 🤑 +70PIPS RUNNING PROFIT - TP HIT ✅" is noise.
3. Gold trades around 1800-5500. A "TP" far outside that range, or wildly distant
   from the entry, is a typo by the provider — omit it rather than passing it on.
4. Entry zones are written as "4039 - 4032". Order does not imply direction; set
   entry_low to the smaller number and entry_high to the larger.
5. If the post is ambiguous about direction, entry, or stop, classify what you are
   sure of and lower your confidence. Do not fill gaps with assumptions.
6. TPs may be absolute prices (4046, 4041) or pip distances (50/100Pips). Set
   tp_unit accordingly. Values under 1000 are pips; values over 1000 are prices."""


# ----------------------------------------------------------------- guardrails


def _numbers_in(text: str) -> set[str]:
    """Every numeric token in the source, normalized (4046.0 == 4046)."""
    out = set()
    for m in re.findall(r"\d+(?:[.,]\d+)?", text.replace(",", "")):
        try:
            out.add(f"{float(m):g}")
        except ValueError:
            pass
    return out


def validate_extraction(sig: LLMSignal, raw: str, cfg: dict,
                        live_price: float | None = None) -> tuple[bool, str]:
    """Every guardrail. Returns (ok, reason). Pure — no API, unit-testable."""
    live = cfg.get("live", {})
    if sig.kind != "entry":
        return True, f"{sig.kind} (no trade)"

    if sig.side is None or sig.entry_low is None or sig.entry_high is None:
        return False, "entry missing side or entry price"
    if sig.sl is None:
        return False, "entry has no numeric stop-loss"
    if not sig.tps:
        return False, "entry has no take-profit"

    # 2. no invented numbers
    present = _numbers_in(raw)
    for label, val in [("entry_low", sig.entry_low), ("entry_high", sig.entry_high),
                       ("sl", sig.sl), *[(f"tp{i}", t) for i, t in enumerate(sig.tps)]]:
        if f"{val:g}" not in present:
            return False, f"hallucinated {label}={val:g} (not in source text)"

    mid = (sig.entry_low + sig.entry_high) / 2
    long = sig.side == "buy"

    # 3. geometry — stop on the losing side, targets on the winning side
    if (sig.sl < mid) != long:
        return False, f"SL {sig.sl:g} on wrong side of entry {mid:g} for a {sig.side}"
    tps_abs = [mid + t * PIP * (1 if long else -1) if sig.tp_unit == "pips" else t
               for t in sig.tps]
    if not any((t > mid) == long for t in tps_abs):
        return False, f"no TP on the profitable side of {mid:g} for a {sig.side}"

    # 4. sanity — stop distance band, targets not absurd
    sl_pips = abs(mid - sig.sl) / PIP
    lo, hi = live.get("sl_pips_min", 0), live.get("sl_pips_max", 10_000)
    if not (lo <= sl_pips <= hi):
        return False, f"SL {sl_pips:.0f}p outside {lo}-{hi}p band"
    if sig.tp_unit == "price" and any(abs(t - mid) > 0.10 * mid for t in sig.tps):
        return False, "a TP is >10% from entry (provider typo)"

    # 5. market proximity
    drift = live.get("max_price_drift_pct", 1.0)
    if live_price and abs(mid - live_price) / live_price * 100 > drift:
        return False, f"entry {mid:g} is >{drift}% from live {live_price:g} (stale signal)"

    return True, "passed all guardrails"


# ----------------------------------------------------------------- the call


def parse_with_llm(text: str, client=None, effort: str = "low") -> LLMSignal | None:
    """One Claude call. Returns None on refusal/error — caller treats that as 'skip'."""
    import anthropic

    client = client or anthropic.Anthropic()
    try:
        resp = client.messages.parse(
            model=MODEL,
            max_tokens=2048,
            # Low effort: this is bounded extraction on a latency-sensitive path.
            # Thinking stays on (Opus 5 default) — disabling it can leak <thinking>
            # tags into output and is capped at `high` effort anyway.
            output_config={"effort": effort},
            system=[{
                "type": "text",
                "text": SYSTEM,
                # Frozen prefix -> cache read on every subsequent signal.
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": f"<post>\n{text.strip()}\n</post>"}],
            output_format=LLMSignal,
        )
    except Exception as exc:                      # network, rate limit, 400 …
        print(f"  [llm] call failed ({type(exc).__name__}) -> skip")
        return None

    if getattr(resp, "stop_reason", None) == "refusal":
        print("  [llm] refusal -> skip")
        return None
    return resp.parsed_output


# ----------------------------------------------------------------- arbitration


def arbitrate(regex_sig: dict | None, llm_sig: LLMSignal | None, raw: str,
              cfg: dict, live_price: float | None = None) -> dict:
    """Combine the two readings into one verdict. Never guesses with money."""
    def fail(reason, **kw):
        return {"decision": "skip", "reason": reason, "source": "arbiter", **kw}

    if llm_sig is None:
        if regex_sig:
            return {"decision": "take", "source": "regex_only",
                    "reason": "llm unavailable, regex parsed cleanly", "signal": regex_sig}
        return fail("neither parser produced a signal")

    ok, why = validate_extraction(llm_sig, raw, cfg, live_price)

    if llm_sig.kind == "management":
        return {"decision": "manage", "source": "llm", "action": llm_sig.action,
                "reason": llm_sig.reason}
    if llm_sig.kind == "noise":
        if regex_sig:
            return fail("DISAGREE: regex saw a signal, llm says noise", conflict=True)
        return {"decision": "ignore", "source": "both", "reason": llm_sig.reason}

    if not ok:
        return fail(f"llm entry rejected: {why}")

    if regex_sig is None:
        return {"decision": "take", "source": "llm_only",
                "reason": f"regex missed it; {why}", "signal": _to_engine(llm_sig)}

    # both saw an entry — they must agree on the tradeable facts
    r_side = regex_sig.get("side")
    r_sl = regex_sig.get("sl")
    if r_side != llm_sig.side:
        return fail(f"DISAGREE on side: regex={r_side} llm={llm_sig.side}", conflict=True)
    if r_sl is not None and llm_sig.sl is not None and abs(r_sl - llm_sig.sl) > 0.01:
        return fail(f"DISAGREE on SL: regex={r_sl} llm={llm_sig.sl}", conflict=True)
    return {"decision": "take", "source": "both_agree", "reason": why, "signal": regex_sig}


def _to_engine(sig: LLMSignal) -> dict:
    """LLMSignal -> the dict shape engine.simulate()/the executor expects."""
    return {
        "side": sig.side, "entry_low": sig.entry_low, "entry_high": sig.entry_high,
        "entry_mid": round((sig.entry_low + sig.entry_high) / 2, 3),
        "sl": sig.sl, "tp_mode": sig.tp_unit, "tp_raw": sig.tps,
        "tp_prices": sig.tps if sig.tp_unit == "price" else [],
        "addon": sig.is_addon, "raw": "", "_llm_confidence": sig.confidence,
    }
