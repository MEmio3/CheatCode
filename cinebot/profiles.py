"""Account profiles for multi-session booking.

A Profile = one real attendee's account: a label (who), the email/username they
log in with, and the phone that receives their OTP. These are real people's
accounts used with consent — not generated identities.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class Profile:
    id: str
    label: str
    email: str
    phone: str


class ProfileStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or os.path.join(
            os.path.expanduser("~"), ".cinebot", "profiles.json"
        )
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def _read(self) -> list[Profile]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as f:
            return [Profile(**p) for p in json.load(f)]

    def _write(self, profiles: list[Profile]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([asdict(p) for p in profiles], f, indent=2)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def list(self) -> list[Profile]:
        return self._read()

    def add(self, label: str, email: str, phone: str) -> Profile:
        p = Profile(id=uuid.uuid4().hex[:10], label=label, email=email, phone=phone)
        profiles = self._read()
        profiles.append(p)
        self._write(profiles)
        return p

    def delete(self, profile_id: str) -> bool:
        profiles = self._read()
        new = [p for p in profiles if p.id != profile_id]
        if len(new) == len(profiles):
            return False
        self._write(new)
        return True
