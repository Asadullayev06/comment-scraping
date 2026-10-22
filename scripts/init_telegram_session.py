"""Helper CLI script to initialize and generate a Telethon MTProto Session String.

Run this script once to authenticate your Telegram userbot account:
    python scripts/init_telegram_session.py

It will generate a TELEGRAM_SESSION_STRING that you can paste into your .env file
or Coolify environment variables.
"""

import os
import sys
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    print("=" * 60)
    print("  Operatives MTProto Userbot Session Initializer")
    print("=" * 60)

    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")

    if not api_id or not api_hash:
        print("\nPlease enter your Telegram API credentials from https://my.telegram.org:")
        api_id = input("Telegram API_ID: ").strip()
        api_hash = input("Telegram API_HASH: ").strip()

    try:
        api_id_int = int(api_id)
    except ValueError:
        print("Error: API_ID must be an integer.")
        sys.exit(1)

    print("\nConnecting to Telegram servers...")
    client = TelegramClient(StringSession(), api_id_int, api_hash)
    await client.start()

    me = await client.get_me()
    session_string = client.session.save()

    print("\n" + "=" * 60)
    print("SUCCESSFULLY AUTHENTICATED!")
    print(f"Logged in as: {me.first_name} (@{me.username or 'No Username'}) [ID: {me.id}]")
    print("=" * 60)
    print("\nCopy and paste this string into your .env file as TELEGRAM_SESSION_STRING:\n")
    print(f"TELEGRAM_SESSION_STRING={session_string}\n")
    print("=" * 60)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
