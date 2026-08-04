import jwt

from personaforge_agent.domain.models import AgentTurn


class JwtDelegationVerifier:
    def __init__(self, secret: str, issuer: str, audience: str) -> None:
        self._secret = secret
        self._issuer = issuer
        self._audience = audience

    def verify(self, token: str, turn: AgentTurn) -> None:
        claims = jwt.decode(
            token,
            self._secret,
            algorithms=["HS256"],
            issuer=self._issuer,
            audience=self._audience,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "jti"]},
        )
        expected = {
            "sub": turn.user_id,
            "session_id": turn.session_id,
            "turn_id": turn.turn_id,
            "agent_id": turn.agent_id,
        }
        if any(claims.get(key) != value for key, value in expected.items()):
            raise PermissionError("Delegation token does not match the requested turn")
        granted_tools = set(_string_list(claims.get("tool_permissions")))
        granted_knowledge = set(_string_list(claims.get("knowledge_base_ids")))
        if not set(turn.tool_permissions).issubset(granted_tools):
            raise PermissionError("Delegation token does not grant requested tools")
        if not set(turn.knowledge_base_ids).issubset(granted_knowledge):
            raise PermissionError("Delegation token does not grant requested knowledge")


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return ()
    return tuple(value)
