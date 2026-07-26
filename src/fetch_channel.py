"""
Fetch 6 months of history from ANY channel into data/raw_<tag>.jsonl.
Generalizes fetch_history.py so we can add more signal providers.

    python fetch_channel.py <chat> <tag> [months_back]
    python fetch_channel.py https://t.me/GoldScalperNinja goldscalperninja 6

Uses the cached signals.session (same Telegram login as Gary).
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import User

load_dotenv()

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION = os.environ.get("TG_SESSION", "signals")
OUT_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_DIR.mkdir(exist_ok=True)


async def main():
    chat = sys.argv[1]
    tag = sys.argv[2]
    months_back = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    cutoff = datetime.now(timezone.utc) - timedelta(days=months_back * 30)

    client = TelegramClient(str(OUT_DIR.parent / SESSION), API_ID, API_HASH)
    await client.start()

    entity = await client.get_entity(chat)
    title = getattr(entity, "title", getattr(entity, "username", str(chat)))
    print(f"Connected. Reading '{title}' back to {cutoff.date()}")

    out_path = OUT_DIR / f"raw_{tag}.jsonl"
    kept = scanned = no_text = 0
    with out_path.open("w", encoding="utf-8") as f:
        async for msg in client.iter_messages(entity):
            if msg.date < cutoff:
                break
            scanned += 1
            sender = await msg.get_sender()
            text = msg.message or ""
            if not text.strip():
                no_text += 1
                continue
            uname = getattr(sender, "username", None)
            name = None
            if isinstance(sender, User):
                name = " ".join(filter(None, [sender.first_name, sender.last_name]))
            f.write(json.dumps({
                "id": msg.id, "date": msg.date.isoformat(),
                "sender_id": getattr(sender, "id", None),
                "sender_username": uname, "sender_name": name,
                "reply_to": msg.reply_to_msg_id, "source": tag, "text": text,
            }, ensure_ascii=False) + "\n")
            kept += 1
            if scanned % 500 == 0:
                print(f"  scanned {scanned}, kept {kept} (at {msg.date.date()})")

    print(f"\nDone. Scanned {scanned}, kept {kept} text msgs "
          f"({no_text} image/empty skipped).")
    print(f"Saved -> {out_path}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
