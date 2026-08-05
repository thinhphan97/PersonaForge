from datetime import datetime, timezone

import jwt

from personaforge_control.domain.errors import InvalidEnrollment
from personaforge_control.domain.models import EnrollmentClaims, EnrollmentGrant


class EnrollmentJwtAdapter:
    def __init__(self, secret: str, issuer: str, audience: str) -> None:
        if len(secret.encode()) < 32:
            raise ValueError("CONTROL_ENROLLMENT_JWT_SECRET must contain at least 32 bytes")
        self._secret = secret
        self._issuer = issuer
        self._audience = audience

    def issue(self, grant: EnrollmentGrant) -> str:
        return jwt.encode(
            {
                "iss": self._issuer,
                "aud": self._audience,
                "sub": grant.subject,
                "jti": grant.token_id,
                "scope": ["worker:register"],
                "cluster_id": grant.cluster_id,
                "host_id": grant.host_id,
                "allowed_worker_type": grant.worker_type,
                "max_gpu_count": grant.max_gpu_count,
                "iat": grant.issued_at,
                "exp": grant.expires_at,
            },
            self._secret,
            algorithm="HS256",
        )

    def decode(self, token: str) -> EnrollmentClaims:
        try:
            data = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": [
                        "sub",
                        "jti",
                        "scope",
                        "cluster_id",
                        "host_id",
                        "allowed_worker_type",
                        "max_gpu_count",
                        "exp",
                    ]
                },
            )
        except jwt.InvalidTokenError as exc:
            raise InvalidEnrollment("Invalid enrollment token") from exc
        scopes = data.get("scope")
        if not isinstance(scopes, list) or "worker:register" not in scopes:
            raise InvalidEnrollment("Enrollment token has no registration scope")
        exp = data.get("exp")
        if not isinstance(exp, int | float):
            raise InvalidEnrollment("Enrollment token expiry is invalid")
        return EnrollmentClaims(
            subject=_required_string(data, "sub"),
            token_id=_required_string(data, "jti"),
            cluster_id=_required_string(data, "cluster_id"),
            host_id=_required_string(data, "host_id"),
            worker_type=_required_string(data, "allowed_worker_type"),
            max_gpu_count=_required_int(data, "max_gpu_count"),
            expires_at=datetime.fromtimestamp(exp, tz=timezone.utc),
        )


def _required_string(data: dict[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise InvalidEnrollment(f"Invalid enrollment claim: {name}")
    return value


def _required_int(data: dict[str, object], name: str) -> int:
    value = data.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidEnrollment(f"Invalid enrollment claim: {name}")
    return value
