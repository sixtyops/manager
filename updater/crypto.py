"""Symmetric encryption for device credentials stored at rest."""

import logging
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_KEY_PATH = Path(__file__).parent.parent / "data" / ".encryption_key"

# Fernet tokens always start with "gAAAAA"
_FERNET_PREFIX = "gAAAAA"

_fernet = None


def _load_fernet() -> Fernet:
    """Load or generate the Fernet key."""
    if _KEY_PATH.exists():
        key = _KEY_PATH.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _KEY_PATH.write_bytes(key + b"\n")
        _KEY_PATH.chmod(0o600)
        logger.info("Generated new device credential encryption key")
    return Fernet(key)


def get_fernet() -> Fernet:
    """Get the singleton Fernet instance."""
    global _fernet
    if _fernet is None:
        _fernet = _load_fernet()
    return _fernet


def key_path() -> Path:
    """Path to the credential encryption key file.

    This key decrypts every stored device password / RADIUS secret. Losing it
    makes those credentials unrecoverable, so backup/restore must keep it with
    the database (see updater/sftp_backup.py).
    """
    return _KEY_PATH


def reset_cache() -> None:
    """Drop the cached Fernet so the next call reloads the key from disk.

    Used after a restore swaps in the backup's key file, so the running process
    decrypts with the restored key without needing a restart.
    """
    global _fernet
    _fernet = None


def encrypt_password(plaintext: str) -> str:
    """Encrypt a device password for storage."""
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_password(ciphertext: str) -> str:
    """Decrypt a stored device password."""
    return get_fernet().decrypt(ciphertext.encode()).decode()


def is_encrypted(value: str) -> bool:
    """Check if a value looks like a Fernet-encrypted token."""
    return value.startswith(_FERNET_PREFIX)
