"""Send a real test message through your Telegram bot (uses the saved credentials).

    python scripts/telegram_test.py                 # uses OS keyring, falling back to secrets.toml
    python scripts/telegram_test.py --save          # prompt for token + chat id and store them

Create a bot with @BotFather, send it any message, then get your chat id from
https://api.telegram.org/bot<TOKEN>/getUpdates  (look for "chat":{"id":...}).
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from xauassist.config.secrets import (
    TelegramSecrets,
    default_secret_store,
    validate_telegram_credentials,
)
from xauassist.notify.telegram import SendOutcome, TelegramClient, UrllibTransport

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", action="store_true", help="prompt for credentials and store them")
    args = parser.parse_args()
    store = default_secret_store(ROOT / "secrets.toml")

    if args.save:
        token = getpass.getpass("Bot token (hidden): ").strip()
        chat_id = input("Chat id: ").strip()
        problems = validate_telegram_credentials(token, chat_id)
        if problems:
            print("\n".join(problems))
            return 2
        store.save(TelegramSecrets(token, chat_id))
        print("Saved.")

    secrets = store.load()
    if secrets is None or not secrets.is_complete:
        print("No Telegram credentials stored. Run with --save.")
        return 1
    client = TelegramClient(secrets, UrllibTransport())
    check = client.get_me()
    print(f"Token check: {check.outcome.value} {check.error}")
    if check.outcome is not SendOutcome.SENT:
        return 1
    result = client.send_message("XAUUSD assistant: test message - Telegram is working.")
    print(f"Send: {result.outcome.value} {result.error}")
    return 0 if result.outcome is SendOutcome.SENT else 1


if __name__ == "__main__":
    sys.exit(main())
