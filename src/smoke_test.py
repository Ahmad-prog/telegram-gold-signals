"""
QUICK END-TO-END TEST — Telegram -> parse -> locked-strategy gates -> alert.

1. Connects with the cached session (signals.session at repo root).
2. Pulls the latest N messages from BOTH channels.
3. Runs each through its parser + the locked `live:` gates from parameters.yml
   (sell-only, fresh-only, SL 40-120p, ladder sizing at rung 0).
4. Prints the ALERT the executor would fire for takeable signals.
5. Then subscribes live (events.NewMessage) for LISTEN_SECS to prove the
   real-time path works, printing any message that arrives.

    python3 src/quick_test.py [n_messages] [listen_secs]
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml
from dotenv import load_dotenv
from telethon import TelegramClient, events

import parse_gary
import parse_gsn

load_dotenv(ROOT / ".env")

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION = str(ROOT / os.environ.get("TG_SESSION", "signals"))

CHANNELS = {
    "Gary_TheTrader": ("gary", parse_gary),
    "GoldScalperNinja": ("gsn", parse_gsn),
}
N_MSGS = int(sys.argv[1]) if len(sys.argv) > 1 else 25
LISTEN_SECS = int(sys.argv[2]) if len(sys.argv) > 2 else 45

LIVE = yaml.safe_load((ROOT / "parameters.yml").read_text())["live"]
PIP = 0.10
ACCOUNT = 100_000


def evaluate(sig, source):
    """Apply the locked gates. Returns (verdict, reason_or_alert_dict)."""
    if sig is None:
        return "not_signal", "no parse (analysis/profit post)"
    if sig["side"] != "sell":
        return "skip", "BUY signal (sell-only strategy)"
    if sig.get("addon"):
        return "skip", "add-on/re-entry (fresh-only)"
    if sig["sl"] is None:
        return "skip", "no SL in message"
    if not sig.get("tp_prices"):
        return "skip", "no parseable TP"
    sl_pips = abs(sig["entry_mid"] - sig["sl"]) / PIP
    if not (LIVE["sl_pips_min"] <= sl_pips <= LIVE["sl_pips_max"]):
        return "skip", f"SL {sl_pips:.0f}p outside {LIVE['sl_pips_min']}-{LIVE['sl_pips_max']}p band"
    # nearest TP for a sell = highest TP below entry -> sorted desc
    tp1 = sorted(sig["tp_prices"], reverse=True)[0]
    risk_pct = LIVE["risk_ladder_pct"][0]          # rung 0 (fresh state)
    risk_usd = ACCOUNT * risk_pct / 100
    lots = max(0.01, round(risk_usd / (sl_pips * 10.0) - 0.004, 2))
    return "TAKE", {
        "source": source, "entry_zone": f"{sig['entry_low']}-{sig['entry_high']}",
        "sl": sig["sl"], "tp1": tp1, "sl_pips": round(sl_pips),
        "risk_pct": risk_pct, "lots": lots,
    }


def show(when, source, text, verdict, info):
    head = text.strip().splitlines()[0][:60] if text.strip() else "(empty)"
    if verdict == "TAKE":
        print(f"  {when:%m-%d %H:%M} [{source:4}] 🚨 ALERT  SELL XAUUSD "
              f"zone {info['entry_zone']}  SL {info['sl']} ({info['sl_pips']}p)  "
              f"TP1 {info['tp1']}  risk {info['risk_pct']}% -> {info['lots']} lots")
    elif verdict == "skip":
        print(f"  {when:%m-%d %H:%M} [{source:4}] skip: {info}  | \"{head}\"")
    # not_signal messages stay silent unless verbose


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: session not authorized — run src/login.py first")
        return
    me = await client.get_me()
    print(f"[1] Telegram OK — logged in as {me.first_name} (@{me.username})")

    counts = {}
    entities = {}
    print(f"\n[2] Replaying last {N_MSGS} messages per channel through the full pipeline:")
    for handle, (source, parser) in CHANNELS.items():
        ent = await client.get_entity(handle)
        entities[ent.id] = (source, parser)
        c = {"TAKE": 0, "skip": 0, "not_signal": 0}
        print(f"\n--- {handle} ---")
        msgs = [m async for m in client.iter_messages(ent, limit=N_MSGS)]
        for m in reversed(msgs):                       # oldest first
            text = m.message or ""
            if not text.strip():
                c["not_signal"] += 1
                continue
            sig = parser.parse(text) if parser.looks_like_signal(text) else None
            verdict, info = evaluate(sig, source)
            c[verdict] += 1
            show(m.date, source, text, verdict, info)
        counts[handle] = c
        print(f"  => {c['TAKE']} alerts, {c['skip']} skipped signals, "
              f"{c['not_signal']} non-signal msgs")

    print(f"\n[3] LIVE subscription test — listening {LISTEN_SECS}s for new posts "
          f"(market may be closed; no arrivals is OK)...")
    got = []

    @client.on(events.NewMessage(chats=list(CHANNELS.keys())))
    async def handler(event):
        src, parser = entities.get(event.chat_id, ("?", None))
        text = event.message.message or ""
        sig = parser.parse(text) if parser and parser.looks_like_signal(text) else None
        verdict, info = evaluate(sig, src)
        got.append(verdict)
        print(f"  LIVE EVENT from {src}: verdict={verdict}")
        show(event.message.date, src, text, verdict, info)

    try:
        await asyncio.wait_for(client.run_until_disconnected(), timeout=LISTEN_SECS)
    except asyncio.TimeoutError:
        pass
    print(f"    subscription was active; {len(got)} live message(s) arrived.")

    print("\n[4] SUMMARY")
    ok = True
    for handle, c in counts.items():
        total = sum(c.values())
        print(f"  {handle}: {total} msgs -> {c['TAKE']} TAKE / {c['skip']} skip / "
              f"{c['not_signal']} non-signal")
        if c["TAKE"] + c["skip"] == 0:
            ok = False
            print(f"    WARNING: no parseable signals at all — check parser/regex")
    print("\n  RESULT:", "✅ end-to-end path works (telegram -> parse -> gates -> alert)"
          if ok else "❌ pipeline issue — see warnings")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
