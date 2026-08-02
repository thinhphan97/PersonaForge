from datetime import datetime, timedelta, timezone

import pytest

from avatar_chat.domain.errors import InvalidStateTransition
from avatar_chat.domain.models import AvatarSession, AvatarState


def make_session() -> AvatarSession:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return AvatarSession("session-1", "default", AvatarState.CREATED, now, now)


def test_happy_path_state_machine() -> None:
    session = make_session()
    later = session.created_at + timedelta(seconds=1)

    session.connected(later)
    assert session.state is AvatarState.IDLE
    session.start_thinking(later)
    session.start_speaking(later)
    session.become_idle(later)
    session.close(later)

    assert session.state is AvatarState.CLOSED


def test_invalid_transition_is_rejected() -> None:
    session = make_session()

    with pytest.raises(InvalidStateTransition):
        session.start_speaking(session.created_at)
