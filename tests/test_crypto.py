import pytest
from cryptography.fernet import InvalidToken

from calsync.crypto import (
    decrypt_token,
    derive_mirror_key,
    encrypt_token,
    generate_fernet_key,
    generate_hmac_key,
)


def test_fernet_roundtrip():
    key = generate_fernet_key()
    plaintext = 'a-google-refresh-token-1//abc123'
    ct = encrypt_token(plaintext, key)
    assert ct != plaintext.encode()
    assert decrypt_token(ct, key) == plaintext


def test_fernet_different_keys_fail():
    k1 = generate_fernet_key()
    k2 = generate_fernet_key()
    ct = encrypt_token('secret', k1)
    with pytest.raises(InvalidToken):
        decrypt_token(ct, k2)


def test_mirror_key_is_deterministic():
    hk = generate_hmac_key()
    args = {
        'source_google_calendar_id': 'edmenendez@gmail.com',
        'source_event_id': 'abc123xyz',
        'target_google_calendar_id': 'ed@beachmedia.io',
        'mode': 'busy',
        'hmac_key': hk,
    }
    assert derive_mirror_key(**args) == derive_mirror_key(**args)


def test_mirror_key_changes_with_inputs():
    hk = generate_hmac_key()
    base = {
        'source_google_calendar_id': 'edmenendez@gmail.com',
        'source_event_id': 'abc123',
        'target_google_calendar_id': 'ed@beachmedia.io',
        'mode': 'busy',
        'hmac_key': hk,
    }
    k0 = derive_mirror_key(**base)
    assert derive_mirror_key(**{**base, 'source_event_id': 'abc124'}) != k0
    assert derive_mirror_key(**{**base, 'target_google_calendar_id': 'ed@avela.org'}) != k0
    assert derive_mirror_key(**{**base, 'mode': 'full'}) != k0
    assert derive_mirror_key(**{**base, 'source_google_calendar_id': 'other@gmail.com'}) != k0


def test_mirror_key_changes_with_secret():
    args = {
        'source_google_calendar_id': 'edmenendez@gmail.com',
        'source_event_id': 'abc123',
        'target_google_calendar_id': 'ed@beachmedia.io',
        'mode': 'busy',
    }
    k1 = derive_mirror_key(**args, hmac_key=generate_hmac_key())
    k2 = derive_mirror_key(**args, hmac_key=generate_hmac_key())
    assert k1 != k2


def test_mirror_key_length_and_charset():
    key = derive_mirror_key(
        source_google_calendar_id='a@b.c',
        source_event_id='evt',
        target_google_calendar_id='x@y.z',
        mode='full',
        hmac_key=generate_hmac_key(),
    )
    assert len(key) == 32
    assert all(c in '0123456789abcdef' for c in key)


def test_mirror_key_rejects_invalid_mode():
    with pytest.raises(ValueError):
        derive_mirror_key(
            source_google_calendar_id='a@b.c',
            source_event_id='evt',
            target_google_calendar_id='x@y.z',
            mode='invalid',
            hmac_key=generate_hmac_key(),
        )


def test_mirror_key_rejects_empty_inputs():
    with pytest.raises(ValueError):
        derive_mirror_key(
            source_google_calendar_id='',
            source_event_id='evt',
            target_google_calendar_id='x@y.z',
            mode='busy',
            hmac_key=generate_hmac_key(),
        )


def test_mirror_key_separator_prevents_collision():
    """Without the ':' separator, ('ab', 'c') and ('a', 'bc') would collide."""
    hk = generate_hmac_key()
    k1 = derive_mirror_key(
        source_google_calendar_id='ab',
        source_event_id='c',
        target_google_calendar_id='x',
        mode='busy',
        hmac_key=hk,
    )
    k2 = derive_mirror_key(
        source_google_calendar_id='a',
        source_event_id='bc',
        target_google_calendar_id='x',
        mode='busy',
        hmac_key=hk,
    )
    assert k1 != k2
