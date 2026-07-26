"""
One-time login helper that can be driven WITHOUT an interactive terminal.

Telethon's normal login prompts on stdin for the code. This version instead
reads the phone from .env (TG_PHONE) and waits for the login code to appear in
a file (login_code.txt), so another process can write it. After a successful
login the .session file is cached and fetch_history.py runs unattended.

Flow:
    1. python login.py            # sends code request, then polls login_code.txt
    2. (you get a code in Telegram) -> write it:  echo 123456 > login_code.txt
    3. login.py picks it up, signs in, exits. Done forever (session cached).

If you have 2FA, also set TG_PASSWORD in .env.
"""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

load_dotenv()

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION = os.environ.get("TG_SESSION", "signals")
PHONE = os.environ["TG_PHONE"]
PASSWORD = os.environ.get("TG_PASSWORD", "")

CODE_FILE = Path(__file__).resolve().parent.parent / "login_code.txt"
SESSION_PATH = Path(__file__).resolve().parent.parent / SESSION


async def wait_for_code(timeout=900):
    """Poll login_code.txt until a code shows up (or timeout)."""
    if CODE_FILE.exists():
        CODE_FILE.unlink()
    print(f"Waiting for login code -> write it to {CODE_FILE.name}", flush=True)
    waited = 0
    while waited < timeout:
        if CODE_FILE.exists():
            code = CODE_FILE.read_text().strip()
            if code:
                CODE_FILE.unlink()
                return code
        await asyncio.sleep(2)
        waited += 2
    raise TimeoutError("No login code received within timeout.")


async def main():
    client = TelegramClient(str(SESSION_PATH), API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already logged in as {me.first_name} (@{me.username}). Nothing to do.")
        await client.disconnect()
        return

    print(f"Requesting login code for {PHONE} ...", flush=True)
    sent = await client.send_code_request(PHONE)

    code = await wait_for_code()
    try:
        await client.sign_in(PHONE, code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        if not PASSWORD:
            raise RuntimeError("2FA enabled but TG_PASSWORD not set in .env")
        await client.sign_in(password=PASSWORD)

    me = await client.get_me()
    print(f"Logged in as {me.first_name} (@{me.username}). Session saved.", flush=True)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
