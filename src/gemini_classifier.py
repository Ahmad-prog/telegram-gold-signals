"""
Gemini classifier — the front door of the live pipeline.

Every Telegram post goes through classify(): it decides ENTRY / UPDATE / NOISE
and extracts the tradeable fields as strict JSON. Per docs/design-doc.md and the
settled pipeline spec:

  ENTRY  -> a new order. Cross-checked against the regex parser, run through the
            shared guardrails in llm_parser.validate_extraction, then placed with
            SL+TP attached to the order itself.
  UPDATE -> an instruction about a trade already open (move SL, close, cancel).
            Correlated back to a trade by msg_id (edit / reply_to), else the one
            open position, else logged and ignored.
  NOISE  -> recaps ("+140 pips TP HIT"), analysis, promos. Dropped.

The model NEVER places a trade on its own: it classifies and extracts, and the
deterministic layers decide. Guardrails live in llm_parser.py and are shared
with the Claude layer so both providers are held to the same standard.

    python3 src/gemini_classifier.py "Gold Sell Now @ 4051 - 4057  Sl: 4062  TP: 4046"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

MODEL = "gemini-3.1-flash-lite"      # cheapest tier with a future; 2.5 retires 2026-10-16
PIP = 0.10


class GeminiSignal(BaseModel):
    """Field names deliberately match llm_parser.LLMSignal so the shared
    guardrails in validate_extraction() accept this object unchanged."""

    kind: Literal["entry", "update", "noise"] = Field(
        description="entry = a NEW order to place now. update = an instruction about a trade "
                    "ALREADY open (move stop, take partial, close, cancel, or a correction to a "
                    "previous signal). noise = anything else: results recaps, analysis, promos."
    )
    side: Literal["buy", "sell", "none"] = Field(
        description="Direction for an entry. 'none' for update/noise."
    )
    entry_low: float = Field(description="Low end of the entry zone, or the single entry price. 0 if absent.")
    entry_high: float = Field(description="High end of the entry zone, or the single entry price. 0 if absent.")
    sl: float = Field(description="Stop-loss as an absolute price. 0 if the post states no numeric stop.")
    tps: list[float] = Field(description="Take-profit values in the order written. Empty list if none.")
    tp_unit: Literal["price", "pips", "none"] = Field(
        description="'price' for absolute gold prices (4046), 'pips' for distances (50/100). 'none' if no TPs."
    )
    is_addon: bool = Field(description="True if this adds to / re-enters an existing position ('again', 'more', 'round 2').")

    action: Literal["open", "modify_sl", "modify_tp", "close_partial",
                    "close_all", "cancel", "none"] = Field(
        description="For an update, what to do with the open trade. 'open' for entries. 'none' for noise."
    )
    new_sl: float = Field(description="For a modify_sl update, the new stop price. 0 if not applicable.")
    new_tp: float = Field(description="For a modify_tp update, the new target price. 0 if not applicable.")
    refers_to_prior_trade: bool = Field(
        description="True if the post talks about a trade already placed rather than opening a new one."
    )
    confidence: float = Field(description="0.0-1.0 confidence in this reading.")
    reason: str = Field(description="One short sentence explaining the classification.")


SYSTEM = """You read Telegram posts from retail XAUUSD (gold) signal providers and turn them into structured data for an automated trading bot.

A regex parser reads the same post independently, and a trade is only placed when both agree. Your job is accuracy, not coverage — a wrong reading costs real money, an abstention costs nothing.

CLASSIFY every post as exactly one of:

- "entry": a NEW order to place now. Names a direction, normally an entry price or zone, usually a numeric stop.
- "update": an explicit INSTRUCTION to act on a trade that is ALREADY open — move the stop, take partial profit, close everything, cancel a pending order, or correct a previous signal ("SL should be 4029 not 4092", "ignore the last one").
- "noise": everything else. Results recaps ("+140PIPS RUNNING PROFIT - TP HIT ✅"), market analysis, chart commentary, education, promotions, greetings, VIP advertising, session summaries.

INSTRUCTION vs COMMENTARY — the most important distinction you make:

An "update" requires an imperative aimed at the reader: "close now", "move your SL to breakeven", "take partial profit", "cancel that order", "adjust your stop".

Commentary ABOUT how a trade is going is "noise", even when it names a direction, a profit figure, or a target being reached. All of these are noise, not updates:
  "HIT TP1 and now running +50pips in profit ☠️"
  "Trade Active Profit Now"
  "Gold moving nicely as mapping plan ✅"
  "Sell Gold on TOP — 70PIPS RUNNING PROFIT - TP HIT ✅"

The bot acts on updates by closing or modifying live positions. Misreading a progress note as a close instruction would exit a winning trade early. When you cannot tell whether a post is an instruction or a description, choose "noise".

Optional or hedged suggestions are also "noise", not updates. A directive is unconditional ("close now", "move your SL to 4055"). These are hedged and therefore noise:
  "Secure almost profit & set breakeven for zero risk if you want."
  "Take profit & set breakeven for zero risk if you want"
  "You can consider trailing your stop here"
Only classify as "update" when the provider is plainly telling followers to do something now, with no "if you want" / "you can" / "consider" hedge.

CRITICAL RULES:

1. NEVER invent a number. Every price you output must appear verbatim in the post. If it says "SL: in LiveTrade" or "TP: open" there is no numeric value — return 0 / an empty list. Never infer, calculate, or round.
2. A post CELEBRATING a result is noise, not an entry, even though it names a direction. "Sell Gold on TOP 🤑 +70PIPS RUNNING PROFIT - TP HIT ✅" is noise. "Woohooo, Sell Gold Waterfall +140PIPS" is noise.
3. Gold trades roughly 1800-5500. A number far outside that, or wildly distant from the entry, is a provider typo — omit it rather than passing it on.
4. Entry zones are written "4039 - 4032". The order does not imply direction: entry_low is the smaller number, entry_high the larger.
5. TPs may be absolute prices (4046, 4041) or pip distances (50/100Pips). Set tp_unit accordingly: values under 1000 are pips, over 1000 are prices.
6. If direction, entry, or stop is ambiguous, extract only what you are sure of and lower your confidence. Do not fill gaps with assumptions.
7. Use 0 for absent numbers and "none" for absent enums. Never use null."""


def _client():
    from dotenv import load_dotenv
    from google import genai
    load_dotenv(ROOT / ".env")
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set (put it in .env)")
    return genai.Client(api_key=key)


def classify(text: str, client=None) -> GeminiSignal | None:
    """One Gemini call. Returns None on any failure — caller treats that as 'skip'."""
    from google.genai import types

    client = client or _client()
    try:
        resp = client.models.generate_content(
            model=MODEL,
            contents=f"<post>\n{text.strip()}\n</post>",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM,
                response_mime_type="application/json",
                response_schema=GeminiSignal,
                temperature=0,           # deterministic extraction, not creative writing
                max_output_tokens=800,
            ),
        )
        return resp.parsed
    except Exception as exc:
        print(f"  [gemini] call failed ({type(exc).__name__}: {str(exc)[:80]}) -> skip")
        return None


def to_guardrail_shape(sig: GeminiSignal):
    """Gemini uses 0/'none' sentinels (its schema disallows null); the shared
    guardrails expect None. Returns a light object with the normalized fields."""
    class _S:
        pass
    s = _S()
    s.kind = "entry" if sig.kind == "entry" else sig.kind
    s.side = None if sig.side == "none" else sig.side
    s.entry_low = sig.entry_low or None
    s.entry_high = sig.entry_high or None
    s.sl = sig.sl or None
    s.tps = list(sig.tps)
    s.tp_unit = None if sig.tp_unit == "none" else sig.tp_unit
    s.is_addon = sig.is_addon
    return s


def main():
    text = " ".join(sys.argv[1:]) or "Gold Sell Now @ 4051 - 4057\nSl: 4062\nTP: 4046, 4041"
    sig = classify(text)
    if sig is None:
        print("classification failed"); return
    print(f"kind={sig.kind}  side={sig.side}  action={sig.action}  conf={sig.confidence:.2f}")
    print(f"entry={sig.entry_low}-{sig.entry_high}  sl={sig.sl}  tps={sig.tps} ({sig.tp_unit})")
    print(f"reason: {sig.reason}")

    if sig.kind == "entry":
        import yaml
        from llm_parser import validate_extraction
        cfg = yaml.safe_load((ROOT / "parameters.yml").read_text())
        ok, why = validate_extraction(to_guardrail_shape(sig), text, cfg)
        print(f"guardrails: {'PASS' if ok else 'REJECT'} — {why}")


if __name__ == "__main__":
    main()
