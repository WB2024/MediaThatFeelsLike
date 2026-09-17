"""
Fernet-encrypted text field for storing service credentials at rest.

Values are stored as `enc:<token>`; anything without the prefix is treated as legacy
plain text and returned as-is, so a row written before the key existed can still be read.
"""

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

PREFIX = "enc:"


def _fernet():
    key = settings.CREDENTIAL_ENCRYPTION_KEY
    if not key:
        raise ImproperlyConfigured(
            "CREDENTIAL_ENCRYPTION_KEY is not set. Generate one with "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
            "and put it in .env."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(value: str) -> str:
    if value is None or value == "":
        return value
    return PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ImproperlyConfigured(
            "Stored credential could not be decrypted -- CREDENTIAL_ENCRYPTION_KEY has "
            "changed since it was saved. Re-enter the credential on the Settings page."
        ) from exc


class EncryptedTextField(models.TextField):
    description = "Text encrypted at rest with Fernet"

    def from_db_value(self, value, expression, connection):
        return decrypt(value)

    def to_python(self, value):
        return decrypt(value) if isinstance(value, str) else value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "" or value.startswith(PREFIX):
            return value
        return encrypt(value)
