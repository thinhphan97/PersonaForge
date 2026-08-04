from dataclasses import dataclass
from enum import Enum


class AgentEventType(str, Enum):
    TURN_STARTED = "turn_started"
    HEARTBEAT = "heartbeat"
    TEXT_DELTA = "text_delta"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"


@dataclass(frozen=True, slots=True)
class AgentMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class AgentTurn:
    turn_id: str
    session_id: str
    user_id: str
    agent_id: str
    agent_config_version: int
    user_message: str
    history: tuple[AgentMessage, ...]
    tool_permissions: tuple[str, ...]
    knowledge_base_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentEvent:
    type: AgentEventType
    sequence: int
    text: str = ""
    stage: str = ""
    error_code: str = ""
    error_detail: str = ""
