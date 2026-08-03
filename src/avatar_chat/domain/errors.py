class DomainError(Exception):
    """Base error for failures caused by business rules."""


class SessionNotFound(DomainError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"Session '{session_id}' was not found")
        self.session_id = session_id


class InvalidStateTransition(DomainError):
    def __init__(self, current: object, target: object) -> None:
        super().__init__(f"Cannot transition avatar from '{current}' to '{target}'")
        self.current = current
        self.target = target


class SessionBusy(DomainError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"Session '{session_id}' is already handling a message")
        self.session_id = session_id


class ConcurrentSessionUpdate(DomainError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"Session '{session_id}' was changed by another operation")
        self.session_id = session_id
