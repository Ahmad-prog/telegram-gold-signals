"""
Read the Telegram login code from the service chat (Telegram, id 777000)
using the cached session — then optionally listen live for a new one.

    python3 src/get_login_code.py [listen_secs]

Login codes are delivered by Telegram's official service account to any
already-authorized session. Prints the most recent code(s) and their age.
"""
import asyncio
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv(ROOT / ".env")
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION = str(ROOT / os.environ.get("TG_SESSION", "signals"))
SERVICE_ID = 777000                      # official "Telegram" service account
CODE_RE = re.compile(r"\b(\d{5,6})\b")
LISTEN = int(sys.argv[1]) if len(sys.argv) > 1 else 90


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: session not authorized")
        return
    me = await client.get_me()
    print(f"session OK — account: {me.first_name} ({me.phone})\n")

    print("--- recent messages from Telegram service (777000) ---")
    found = []
    async for m in client.iter_messages(SERVICE_ID, limit=8):
        txt = (m.message or "").replace("\n", " ")
        age = (datetime.now(timezone.utc) - m.date).total_seconds()
        codes = CODE_RE.findall(txt)
        tag = f"  <== CODE {codes[0]}" if codes and "code" in txt.lower() else ""
        print(f"  [{m.date:%Y-%m-%d %H:%M:%S} UTC | {age/60:5.1f} min ago] {txt[:110]}{tag}")
        if codes and "code" in txt.lower():
            found.append((m.date, codes[0], age))

    if found:
        d, code, age = found[0]
        print(f"\n>>> LATEST LOGIN CODE: {code}   (received {age/60:.1f} min ago)")
        if age > 300:
            print("    ⚠️  older than 5 min — Telegram codes expire quickly; request a new one")
    else:
        print("\n(no login code found in recent service messages)")

    print(f"\n--- listening {LISTEN}s for a NEW code (trigger the login now) ---")
    got = asyncio.Event()

    @client.on(events.NewMessage(chats=SERVICE_ID))
    async def handler(event):
        txt = (event.message.message or "").replace("\n", " ")
        codes = CODE_RE.findall(txt)
        print(f"  NEW [{event.message.date:%H:%M:%S}] {txt[:120]}")
        if codes and "code" in txt.lower():
            print(f"\n>>> NEW LOGIN CODE: {codes[0]}")
            got.set()

    try:
        await asyncio.wait_for(got.wait(), timeout=LISTEN)
    except asyncio.TimeoutError:
        print("  (no new code arrived in the listen window)")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
