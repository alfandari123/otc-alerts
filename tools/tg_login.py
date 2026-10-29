"""One-time login of Reuven's Telegram USER account for the group reader.

Run in a visible console; HE types phone / code / 2FA password himself.
Saves a StringSession to ~/.otc-secrets/tg_session.txt (never committed).
"""
import json
import os
import sys

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

SECRETS = os.path.join(os.path.expanduser("~"), ".otc-secrets")


def main():
    cfg = json.load(open(os.path.join(SECRETS, "tg_user.json"), encoding="utf-8-sig"))
    print("=" * 50)
    print(" Telegram login - type your phone like +9725XXXXXXXX")
    print(" Then the code Telegram sends you, and your 2FA password if you have one.")
    print("=" * 50)
    with TelegramClient(StringSession(), cfg["api_id"], cfg["api_hash"],
                        device_model="Alfa Group Reader", system_version="Windows") as client:
        client.start()
        me = client.get_me()
        with open(os.path.join(SECRETS, "tg_session.txt"), "w", encoding="utf-8") as f:
            f.write(client.session.save())
        print(f"\nOK - logged in as {me.first_name}. Session saved. You can close this window.")
    input("\nPress Enter to close...")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e)
        input("Press Enter to close...")
        sys.exit(1)
