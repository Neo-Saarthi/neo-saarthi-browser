import json
import os
import re
from typing import Dict

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()

_primary_key = os.getenv("ENCRYPTION_KEY")
if not _primary_key:
    raise RuntimeError("ENCRYPTION_KEY environment variable is not set.")
_primary_key_id = os.getenv("MF_SAARTHI_ENCRYPTION_KEY_ID", "v1")
if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", _primary_key_id):
    raise RuntimeError("MF_SAARTHI_ENCRYPTION_KEY_ID is invalid.")
_key_values: Dict[str, str] = {_primary_key_id: _primary_key}
_configured_keyring = os.getenv("MF_SAARTHI_ENCRYPTION_KEYS")
if _configured_keyring:
    parsed_keyring = json.loads(_configured_keyring)
    if not isinstance(parsed_keyring, dict):
        raise RuntimeError("MF_SAARTHI_ENCRYPTION_KEYS must be a JSON object.")
    for key_id, key_value in parsed_keyring.items():
        if not isinstance(key_id, str) or not isinstance(key_value, str):
            raise RuntimeError("Encryption key IDs and values must be strings.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", key_id):
            raise RuntimeError("An encryption key ID is invalid.")
        _key_values[key_id] = key_value
_fernets = {key_id: Fernet(value.encode()) for key_id, value in _key_values.items()}
_ENCRYPTED_VALUE_PREFIX = "key:"
_ENCRYPTED_TEXT_PREFIX = "enc:"


def encrypt(value: str) -> str:
    return encrypt_bytes(value.encode()).decode()


def decrypt(value: str) -> str:
    return decrypt_bytes(value.encode()).decode()


def encrypt_bytes(value: bytes) -> bytes:
    token = _fernets[_primary_key_id].encrypt(value)
    return f"{_ENCRYPTED_VALUE_PREFIX}{_primary_key_id}:".encode() + token


def decrypt_bytes(value: bytes) -> bytes:
    prefix = _ENCRYPTED_VALUE_PREFIX.encode()
    if value.startswith(prefix):
        key_id_bytes, token = value[len(prefix) :].split(b":", 1)
        key_id = key_id_bytes.decode()
        fernet = _fernets.get(key_id)
        if fernet is None:
            raise RuntimeError(f"Encryption key {key_id!r} is unavailable.")
        return fernet.decrypt(token)
    for fernet in _fernets.values():
        try:
            return fernet.decrypt(value)
        except InvalidToken:
            continue
    raise InvalidToken


def encrypt_text(value: str) -> str:
    return f"{_ENCRYPTED_TEXT_PREFIX}{encrypt(value)}"


def decrypt_text(value: str) -> str:
    if not value.startswith(_ENCRYPTED_TEXT_PREFIX):
        return value
    encrypted_value = value[len(_ENCRYPTED_TEXT_PREFIX) :]
    if encrypted_value.startswith("v1:"):
        encrypted_value = encrypted_value[len("v1:") :]
    return decrypt(encrypted_value)
