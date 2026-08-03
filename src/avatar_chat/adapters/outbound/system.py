import hashlib
import secrets
from datetime import datetime, timezone
from uuid import uuid4

from pwdlib import PasswordHash


class UuidGenerator:
    def new_id(self) -> str:
        return uuid4().hex


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class Argon2PasswordHasher:
    def __init__(self) -> None:
        self._password_hash = PasswordHash.recommended()

    def hash(self, password: str) -> str:
        return self._password_hash.hash(password)

    def verify(self, password: str, password_hash: str) -> bool:
        return self._password_hash.verify(password, password_hash)


class SecureTokenGenerator:
    def generate(self) -> str:
        return secrets.token_urlsafe(32)

    def hash(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()
