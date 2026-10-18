import secrets
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from core.settings import Settings

hasher = PasswordHasher()
# Equalize expensive work when a username does not exist.
DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain 12–1024 characters")
    return hasher.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    try:
        return hasher.verify(encoded, password)
    except (InvalidHashError, VerificationError):
        return False


def load_jwt_secret(settings: Settings) -> str:
    import hashlib
    import hmac
    epoch = settings.data_dir / "jwt-generation"
    def effective(secret):
        return hmac.new(secret.encode(), epoch.read_bytes(), hashlib.sha256).hexdigest() if epoch.exists() else secret
    if settings.jwt_secret.get_secret_value():
        secret = settings.jwt_secret.get_secret_value()
        if len(secret) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters")
        return effective(secret)
    path = settings.data_dir / "jwt.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(48))
        path.chmod(0o600)
    except FileExistsError:
        pass
    return effective(path.read_text(encoding="utf-8"))


def token_for(user, secret: str, minutes: int) -> str:
    current = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user["id"]),
            "ver": user["token_version"],
            "iat": current,
            "exp": current + timedelta(minutes=minutes),
            "iss": "synaxis",
            "aud": "synaxis-api",
        },
        secret,
        algorithm="HS256",
    )


def decode_token(token, secret):
    return jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        issuer="synaxis",
        audience="synaxis-api",
        options={"require": ["sub", "ver", "iat", "exp", "iss", "aud"]},
    )
