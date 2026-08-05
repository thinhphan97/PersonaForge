import hashlib
import secrets
from datetime import datetime, timezone


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class SecureSecrets:
    def generate(self) -> str:
        return secrets.token_urlsafe(32)

    def hash(self, value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()
