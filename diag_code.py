"""Diagnostic: request a login code and print exactly where Telegram sent it."""
import asyncio, os
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION = os.environ.get("TG_SESSION", "signals")
PHONE = os.environ["TG_PHONE"]


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"ALREADY LOGGED IN as {me.first_name} (@{me.username})")
        await client.disconnect()
        return
    print(f"Sending code request for {PHONE} ...", flush=True)
    sent = await client.send_code_request(PHONE)
    # sent.type tells us delivery channel; next_type is the fallback
    print("DELIVERY TYPE :", type(sent.type).__name__)
    print("FULL sent.type:", sent.type)
    print("NEXT fallback :", sent.next_type)
    print("phone_code_hash present:", bool(sent.phone_code_hash))
    await client.disconnect()


asyncio.run(main())
