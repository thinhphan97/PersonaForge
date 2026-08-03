from datetime import datetime, timedelta, timezone

import pytest

from avatar_chat.domain.errors import InvalidStateTransition
from avatar_chat.domain.models import AvatarSession, AvatarState, SessionStatus


def make_session() -> AvatarSession:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return AvatarSession(
        "session-1",
        "default",
        SessionStatus.CREATED,
        AvatarState.IDLE,
        now,
        now,
    )


def test_happy_path_state_machine() -> None:
    session = make_session()
    later = session.created_at + timedelta(seconds=1)

    session.start_media(later)
    session.media_ready(later)
    assert session.status is SessionStatus.READY
    assert session.avatar_state is AvatarState.IDLE
    session.start_thinking(later)
    session.start_speaking(later)
    session.become_idle(later)
    session.close(later)

    assert session.status is SessionStatus.CLOSED


def test_invalid_transition_is_rejected() -> None:
    session = make_session()

    with pytest.raises(InvalidStateTransition):
        session.start_speaking(session.created_at)
