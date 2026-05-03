"""Crypto helpers for token encryption and mirror_key derivation.

Two independent secrets:

- Fernet key: encrypts OAuth refresh tokens at rest in SQLite. Loss means
  forced re-OAuth for every account but no data corruption.

- HMAC key: derives the deterministic, opaque `calsync_mirror_key` stamped
  on every Google mirror event. Loss means we can no longer recompute keys
  for existing mirrors and must wipe-and-rebuild on recovery (see plan).

Both come from `Settings`, which reads them from the environment.
"""

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet


def generate_fernet_key() -> str:
    return Fernet.generate_key().decode('utf-8')


def generate_hmac_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8')


def encrypt_token(plaintext: str, fernet_key: str) -> bytes:
    return Fernet(fernet_key.encode('utf-8')).encrypt(plaintext.encode('utf-8'))


def decrypt_token(ciphertext: bytes, fernet_key: str) -> str:
    return Fernet(fernet_key.encode('utf-8')).decrypt(ciphertext).decode('utf-8')


def derive_mirror_key(
    *,
    source_google_calendar_id: str,
    source_event_id: str,
    target_google_calendar_id: str,
    mode: str,
    hmac_key: str,
) -> str:
    """Compute the deterministic opaque key stamped on every mirror event.

    All inputs MUST be stable Google identifiers (not SQLite PKs). The
    output is HMAC-SHA256 truncated to 32 hex chars: opaque to admins
    without the secret, deterministic for our service.
    """
    if not source_google_calendar_id or not source_event_id or not target_google_calendar_id or not mode:
        raise ValueError('all mirror_key inputs are required and non-empty')
    if mode not in {'full', 'busy'}:
        raise ValueError(f'invalid mode: {mode!r}')

    payload = f'{source_google_calendar_id}:{source_event_id}:{target_google_calendar_id}:{mode}'
    digest = hmac.new(
        key=base64.urlsafe_b64decode(hmac_key),
        msg=payload.encode('utf-8'),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return digest[:32]
