import asyncio
import hashlib
import re
import secrets

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from email_validator import EmailNotValidError, validate_email

from .errors import ServiceError


PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
EMAIL_RESEND_SECONDS = 60
EMAIL_TOKEN_SECONDS = {"verify": 600, "reset": 1800, "change": 600}
EMAIL_TOKEN_PREFIXES = {"verify": "ev", "reset": "pr", "change": "pc"}
REMEMBER_SECONDS = 30 * 24 * 60 * 60


def email_address(value: str) -> str:
    try:
        return validate_email(value.strip(), check_deliverability=False).normalized.lower()
    except (EmailNotValidError, AttributeError):
        raise ServiceError(422, "invalid_email", "邮箱格式无效") from None


def validate_password(value: str):
    if (
        not isinstance(value, str) or not PASSWORD_MIN_LENGTH <= len(value) <= PASSWORD_MAX_LENGTH
        or not re.search(r"[0-9]", value) or not re.search(r"[a-z]", value)
    ):
        raise ServiceError(422, "weak_password", "密码须为 8 到 128 个字符，并包含数字和小写英文字母")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def token(prefix: str) -> str:
    return prefix + "_" + secrets.token_urlsafe(32)


def token_hash(value: str, prefix: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(re.escape(prefix) + r"_[A-Za-z0-9_-]{43}", value):
        # Invalid input still follows the same database lookup path.
        return digest("invalid-token")
    return digest(value)


class Passwords:
    def __init__(self):
        self.hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.ID)
        self.dummy = self.hasher.hash(secrets.token_urlsafe(32))
        self.capacity = asyncio.Semaphore(4)

    async def hash(self, password):
        validate_password(password)
        async with self.capacity:
            return await asyncio.to_thread(self.hasher.hash, password)

    async def verify(self, encoded, password):
        def check():
            try:
                return self.hasher.verify(encoded or self.dummy, password)
            except (VerificationError, InvalidHashError):
                return False

        async with self.capacity:
            return await asyncio.to_thread(check)
