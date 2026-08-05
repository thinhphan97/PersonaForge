class DomainError(Exception):
    """Base error for failures caused by business rules."""


class SessionNotFound(DomainError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"Session '{session_id}' was not found")
        self.session_id = session_id


class CharacterNotFound(DomainError):
    def __init__(self, character_id: str) -> None:
        super().__init__(f"Character '{character_id}' was not found")
        self.character_id = character_id


class AuthenticationFailed(DomainError):
    def __init__(self) -> None:
        super().__init__("Email/username or password is incorrect")


class AuthenticationRequired(DomainError):
    def __init__(self) -> None:
        super().__init__("Authentication is required")


class UserAlreadyExists(DomainError):
    def __init__(self) -> None:
        super().__init__("Email or username is already registered")


class UserNotFound(DomainError):
    def __init__(self, username: str) -> None:
        super().__init__(f"User '{username}' was not found")


class AccessDenied(DomainError):
    def __init__(self) -> None:
        super().__init__("Access denied")


class InvalidInput(DomainError):
    pass


class RateLimitExceeded(DomainError):
    def __init__(self) -> None:
        super().__init__("Too many authentication attempts; try again later")


class InvalidStateTransition(DomainError):
    def __init__(self, current: object, target: object) -> None:
        super().__init__(f"Cannot transition avatar from '{current}' to '{target}'")
        self.current = current
        self.target = target


class SessionBusy(DomainError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"Session '{session_id}' is already handling a message")
        self.session_id = session_id


class SessionClosed(DomainError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"Session '{session_id}' is closed and read-only")
        self.session_id = session_id


class ConcurrentSessionUpdate(DomainError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"Session '{session_id}' was changed by another operation")
        self.session_id = session_id


class ConcurrentMessageUpdate(DomainError):
    def __init__(self, message_id: str) -> None:
        super().__init__(f"Message '{message_id}' was changed by another operation")
        self.message_id = message_id
