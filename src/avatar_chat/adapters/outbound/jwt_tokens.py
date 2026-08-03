from datetime import datetime, timedelta

import jwt

from avatar_chat.application.dto import AccessTokenClaims, RefreshTokenClaims
from avatar_chat.domain.errors import AuthenticationRequired


class PyJwtTokenAdapter:
    """Signs JWTs while keeping JWT-specific details outside the application core."""

    def __init__(
        self,
        secret: str,
        issuer: str,
        audience: str,
        access_ttl: timedelta,
        refresh_ttl: timedelta,
    ) -> None:
        if len(secret.encode()) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 bytes")
        self._secret = secret
        self._issuer = issuer
        self._audience = audience
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl

    def issue_access(
        self,
        user_id: str,
        session_id: str,
        issued_at: datetime,
    ) -> str:
        return self._encode(
            {
                "sub": user_id,
                "sid": session_id,
                "type": "access",
                "iat": issued_at,
                "exp": issued_at + self._access_ttl,
            }
        )

    def issue_refresh(
        self,
        user_id: str,
        session_id: str,
        family_id: str,
        issued_at: datetime,
    ) -> str:
        return self._encode(
            {
                "sub": user_id,
                "sid": session_id,
                "family": family_id,
                "type": "refresh",
                "iat": issued_at,
                "exp": issued_at + self._refresh_ttl,
            }
        )

    def decode_access(self, token: str) -> AccessTokenClaims:
        claims = self._decode(token, "access")
        return AccessTokenClaims(
            self._required_string(claims, "sub"),
            self._required_string(claims, "sid"),
        )

    def decode_refresh(self, token: str) -> RefreshTokenClaims:
        claims = self._decode(token, "refresh")
        return RefreshTokenClaims(
            self._required_string(claims, "sub"),
            self._required_string(claims, "sid"),
            self._required_string(claims, "family"),
        )

    def _encode(self, claims: dict[str, object]) -> str:
        return jwt.encode(
            {**claims, "iss": self._issuer, "aud": self._audience},
            self._secret,
            algorithm="HS256",
        )

    def _decode(self, token: str, expected_type: str) -> dict[str, object]:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["sub", "sid", "type", "iat", "exp"]},
            )
        except jwt.InvalidTokenError as exc:
            raise AuthenticationRequired() from exc
        if claims.get("type") != expected_type:
            raise AuthenticationRequired()
        return claims

    @staticmethod
    def _required_string(claims: dict[str, object], name: str) -> str:
        value = claims.get(name)
        if not isinstance(value, str) or not value:
            raise AuthenticationRequired()
        return value
