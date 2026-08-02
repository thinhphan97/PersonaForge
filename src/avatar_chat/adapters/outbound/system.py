from datetime import datetime, timezone
from uuid import uuid4


class UuidGenerator:
    def new_id(self) -> str:
        return uuid4().hex


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)
