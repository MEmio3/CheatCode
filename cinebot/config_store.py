"""Encrypted credential store for CineBot.

Primary backend is the OS keyring (Windows Credential Manager / macOS
Keychain / Freedesktop Secret Service). If keyring is unavailable we fall back
to a mode-0600 JSON file with a loud warning — correct-by-default, but the
project still works before `keyring` is pip-installed.

bKash PIN is deliberately NOT a known field and cannot be stored here. The bot
never types the PIN; payment is authorized by the user's own OTP at the gateway.
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import stat
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("cinebot.config_store")

SERVICE = "cinebot"

# The only credentials CineBot ever needs. bKash PIN is intentionally absent.
FIELDS: dict[str, str] = {
    "cineplex_phone": "Cineplex login phone number (e.g. +8801XXXXXXXXX)",
    "bkash_number": "bKash account number (pre-filled at the payment step)",
    "telegram_bot_token": "Telegram bot token (OTP relay; optional)",
    "telegram_chat_id": "Your Telegram chat id (OTP relay; optional)",
}
SENSITIVE = {"telegram_bot_token"}
# Guard rail: these keys can never be set, ever.
FORBIDDEN = {"bkash_pin", "pin", "password", "card_cvv", "cvv"}


def telegram_env_credentials() -> tuple[Optional[str], Optional[str]]:
    """Read optional personal-tool Telegram settings from environment/.env."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    token = os.getenv("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("userID") or os.getenv("USER_ID") or os.getenv("TELEGRAM_CHAT_ID")
    return token.strip() if token else None, chat_id.strip() if chat_id else None


class CredentialBackend(ABC):
    @abstractmethod
    def get(self, key: str) -> Optional[str]: ...
    @abstractmethod
    def set(self, key: str, value: str) -> None: ...
    @abstractmethod
    def delete(self, key: str) -> bool: ...
    @abstractmethod
    def list_keys(self) -> list[str]: ...
    @abstractmethod
    def name(self) -> str: ...


class KeyringBackend(CredentialBackend):
    def __init__(self) -> None:
        import keyring  # lazy so the module imports even without keyring

        self._kr = keyring

    def get(self, key: str) -> Optional[str]:
        v = self._kr.get_password(SERVICE, key)
        return v

    def set(self, key: str, value: str) -> None:
        self._kr.set_password(SERVICE, key, value)

    def delete(self, key: str) -> bool:
        try:
            self._kr.delete_password(SERVICE, key)
            return True
        except self._kr.PasswordDeleteError:
            return False

    def list_keys(self) -> list[str]:
        return list(FIELDS)  # keyring has no enumeration; report known fields

    def name(self) -> str:
        return "os-keyring"


class FileBackend(CredentialBackend):
    """Mode-0600 JSON fallback. Less secure than the OS keyring."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or os.path.join(
            os.path.expanduser("~"), ".cinebot", "creds.json"
        )
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def _read(self) -> dict[str, str]:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: dict[str, str]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)  # 0600

    def get(self, key: str) -> Optional[str]:
        return self._read().get(key)

    def set(self, key: str, value: str) -> None:
        data = self._read()
        data[key] = value
        self._write(data)

    def delete(self, key: str) -> bool:
        data = self._read()
        if key not in data:
            return False
        del data[key]
        self._write(data)
        return True

    def list_keys(self) -> list[str]:
        return list(self._read())

    def name(self) -> str:
        return f"file({self.path})"


@dataclass
class CredentialStore:
    backend: CredentialBackend

    @classmethod
    def auto(cls) -> "CredentialStore":
        try:
            backend: CredentialBackend = KeyringBackend()
            # smoke-test that the backend actually works (Windows may have no
            # default credential helper in some headless setups)
            backend.get("__cinebot_probe__")
            return cls(backend=backend)
        except Exception as e:  # ImportError, MissingBackend, etc.
            log.warning(
                "OS keyring unavailable (%s); falling back to file storage at "
                "~/.cinebot/creds.json (mode 0600). Install `keyring` for secure "
                "storage.",
                type(e).__name__,
            )
            return cls(backend=FileBackend())

    def _check(self, key: str) -> None:
        if key in FORBIDDEN:
            raise ValueError(
                f"Refusing to store {key!r}: CineBot never stores payment PINs "
                f"or CVVs. Payment is authorized via your own OTP at the gateway."
            )
        if key not in FIELDS:
            raise ValueError(f"Unknown credential {key!r}. Known: {sorted(FIELDS)}")

    def set(self, key: str, value: str) -> None:
        self._check(key)
        self.backend.set(key, value)

    def get(self, key: str) -> Optional[str]:
        self._check(key)
        return self.backend.get(key)

    def require(self, key: str) -> str:
        v = self.get(key)
        if not v:
            raise KeyError(f"Missing credential {key!r}. Run: python -m cinebot.config_store set")
        return v

    def delete(self, key: str) -> bool:
        self._check(key)
        return self.backend.delete(key)

    def show(self) -> dict[str, str]:
        """All known fields with secrets masked."""
        out: dict[str, str] = {}
        for k in FIELDS:
            v = self.backend.get(k)
            if k in SENSITIVE:
                out[k] = ("<set>" if v else "<empty>")
            else:
                out[k] = (v if v else "<empty>")
        return out


# ---- CLI -----------------------------------------------------------------====


def _mask(v: Optional[str]) -> str:
    if not v:
        return "<empty>"
    if len(v) <= 4:
        return "*" * len(v)
    return v[:2] + "*" * (len(v) - 4) + v[-2:]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    p = argparse.ArgumentParser(description="CineBot credential store")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("set", help="prompt for and store each known credential")
    g = sub.add_parser("get", help="print a credential value")
    g.add_argument("key", choices=sorted(FIELDS))
    sub.add_parser("show", help="show all credentials (secrets masked)")
    d = sub.add_parser("delete", help="delete a credential")
    d.add_argument("key", choices=sorted(FIELDS))
    sub.add_parser("backend", help="print which storage backend is in use")

    args = p.parse_args()
    store = CredentialStore.auto()

    if args.cmd == "backend":
        print(store.backend.name())
        return 0

    if args.cmd == "set":
        print(f"Backend: {store.backend.name()}")
        print("Enter values (Enter to skip). bKash PIN is NOT stored, ever.\n")
        for k, label in FIELDS.items():
            cur = store.backend.get(k)
            prompt = f"{label} [{_mask(cur) if cur else 'empty'}]: "
            val = getpass.getpass(prompt) if k in SENSITIVE else input(prompt)
            val = val.strip()
            if val:
                store.set(k, val)
                print(f"  saved {k}")
            elif cur:
                print(f"  kept existing {k}")
            else:
                print(f"  skipped {k}")
        print("\nDone. Review with: python -m cinebot.config_store show")
        return 0

    if args.cmd == "get":
        v = store.get(args.key)
        if v is None:
            print(f"{args.key} is not set", file=sys.stderr)
            return 1
        print(v)
        return 0

    if args.cmd == "show":
        for k, v in store.show().items():
            print(f"{k:22} {v}")
        print(f"\nbackend: {store.backend.name()}")
        return 0

    if args.cmd == "delete":
        ok = store.delete(args.key)
        print("deleted" if ok else "not set")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
