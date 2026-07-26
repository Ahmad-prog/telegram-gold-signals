"""
Helper: list all your Telegram chats so you can find the exact group and
Bobby FX's @username / user id. Run this BEFORE fetch_history.py.

    python list_chats.py            # lists your groups/channels
    python list_chats.py "bobby"    # also lists senders in the first chat
                                    # whose title matches "bobby"

Copy the chat's username (or id) into TG_CHAT in .env, and once you spot
Bobby FX's handle in a chat's recent messages, put it in TG_SENDER.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION = os.environ.get("TG_SESSION", "signals")


async def main():
    needle = sys.argv[1].lower() if len(sys.argv) > 1 else None

    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()

    print(f"\n{'id':>15}  {'username':<22} title")
    print("-" * 70)
    match_entity = None
    async for dialog in client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel):
            continue
        ent = dialog.entity
        uname = getattr(ent, "username", None) or ""
        print(f"{dialog.id:>15}  @{uname:<21} {dialog.title}")
        if needle and needle in dialog.title.lower() and match_entity is None:
            match_entity = ent

    # If a chat title matched, show recent senders so you can grab Bobby's handle.
    if match_entity is not None:
        print(f"\nRecent senders in '{match_entity.title}':")
        print("-" * 70)
        seen = {}
        async for msg in client.iter_messages(match_entity, limit=200):
            s = await msg.get_sender()
            if s is None:
                continue
            uname = getattr(s, "username", None) or "(no username)"
            name = getattr(s, "first_name", "") or getattr(s, "title", "")
            seen[s.id] = (uname, name)
        for sid, (uname, name) in seen.items():
            print(f"  id={sid:<14} @{uname:<20} {name}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
