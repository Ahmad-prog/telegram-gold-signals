"""
Fetch historical Telegram messages from a signal provider (e.g. "Bobby FX").

STEP 1 of the pipeline: gather raw data only.
No parsing, no trading. We dump every message to a JSONL file so we can
inspect the real format before building a parser.

Usage:
    python fetch_history.py

Config comes from a .env file (see .env.example). First run will prompt for a
phone-login code from Telegram; after that the session is cached in a .session
file so it won't ask again.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import User

load_dotenv()

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION = os.environ.get("TG_SESSION", "signals")

# The group/channel to read. Can be:
#   - a @username           e.g. "bobbyfxsignals"
#   - a t.me link           e.g. "https://t.me/bobbyfxsignals"
#   - a numeric chat id     e.g. -1001234567890
CHAT = os.environ["TG_CHAT"]

# Optional: only keep messages from this sender. Match by @username (no @) or
# numeric user id. Leave blank to capture ALL messages in the chat.
SENDER_FILTER = os.environ.get("TG_SENDER", "").strip()

# How far back to pull.
MONTHS_BACK = int(os.environ.get("TG_MONTHS_BACK", "6"))

OUT_DIR = Path(__file__).parent / "data"
OUT_DIR.mkdir(exist_ok=True)


def sender_matches(sender) -> bool:
    """Return True if this message's sender passes SENDER_FILTER."""
    if not SENDER_FILTER:
        return True
    if sender is None:
        return False
    if SENDER_FILTER.isdigit():
        return sender.id == int(SENDER_FILTER)
    uname = getattr(sender, "username", None) or ""
    return uname.lower() == SENDER_FILTER.lower().lstrip("@")


async def main():
    cutoff = datetime.now(timezone.utc) - timedelta(days=MONTHS_BACK * 30)

    client = TelegramClient(str(OUT_DIR.parent / SESSION), API_ID, API_HASH)
    await client.start()  # prompts for phone + code on first run only

    entity = await client.get_entity(CHAT)
    title = getattr(entity, "title", getattr(entity, "username", str(CHAT)))
    print(f"Connected. Reading '{title}' back to {cutoff.date()} "
          f"(filter sender={SENDER_FILTER or 'ALL'})")

    out_path = OUT_DIR / "raw_messages.jsonl"
    kept = scanned = 0

    with out_path.open("w", encoding="utf-8") as f:
        # iter_messages walks newest -> oldest; stop once we pass the cutoff.
        async for msg in client.iter_messages(entity):
            if msg.date < cutoff:
                break
            scanned += 1

            sender = await msg.get_sender()
            if not sender_matches(sender):
                continue

            # Skip messages with no text (pure images/stickers) for now —
            # signals are text. We still note them in the count.
            text = msg.message or ""
            if not text.strip():
                continue

            uname = getattr(sender, "username", None)
            name = None
            if isinstance(sender, User):
                name = " ".join(filter(None, [sender.first_name, sender.last_name]))

            record = {
                "id": msg.id,
                "date": msg.date.isoformat(),
                "sender_id": getattr(sender, "id", None),
                "sender_username": uname,
                "sender_name": name,
                "reply_to": msg.reply_to_msg_id,
                "text": text,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            kept += 1

            if scanned % 500 == 0:
                print(f"  scanned {scanned}, kept {kept} (at {msg.date.date()})")

    print(f"\nDone. Scanned {scanned} messages, kept {kept}.")
    print(f"Saved -> {out_path}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
