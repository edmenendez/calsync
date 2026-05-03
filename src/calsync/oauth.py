"""OAuth 2.0 helpers for Google Calendar.

Pure functions; no FastAPI or DB dependencies. Composed by api/oauth.py.

State token format: `<account_label>.<expiry_unix>.<hmac>` where the HMAC
is computed over `<account_label>.<expiry_unix>` with the admin token as
key. Compact, stateless, and survives any process restart.
"""

import base64
import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx

GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v2/userinfo'

CALENDAR_SCOPE = 'https://www.googleapis.com/auth/calendar'

STATE_TTL_SECONDS = 600


def make_state(account_label: str, secret: str, *, now: int | None = None) -> str:
    expiry = (now or int(time.time())) + STATE_TTL_SECONDS
    payload = f'{account_label}.{expiry}'
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b'=').decode()
    return f'{payload}.{sig_b64}'


def verify_state(state: str, secret: str, *, now: int | None = None) -> str:
    """Return the account_label if valid; raise ValueError otherwise."""
    try:
        account_label, expiry_str, sig_b64 = state.rsplit('.', 2)
        expiry = int(expiry_str)
    except (ValueError, AttributeError) as e:
        raise ValueError('malformed state token') from e

    expected_sig = hmac.new(secret.encode(), f'{account_label}.{expiry}'.encode(), hashlib.sha256).digest()
    expected_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b'=').decode()
    if not hmac.compare_digest(sig_b64, expected_b64):
        raise ValueError('invalid state signature')

    if (now or int(time.time())) >= expiry:
        raise ValueError('state expired')

    return account_label


def build_auth_url(*, account_label: str, client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': f'openid email {CALENDAR_SCOPE}',
        'access_type': 'offline',
        'prompt': 'consent',
        'include_granted_scopes': 'true',
        'state': state,
    }
    return f'{GOOGLE_AUTH_URL}?{urlencode(params)}'


async def exchange_code(*, code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict:
    """Exchange authorization code for access + refresh tokens.

    Google may omit `refresh_token` from the response on subsequent
    authorizations; callers must NOT clobber an existing stored refresh
    token if it isn't present here.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                'code': code,
                'client_id': client_id,
                'client_secret': client_secret,
                'redirect_uri': redirect_uri,
                'grant_type': 'authorization_code',
            },
            headers={'Accept': 'application/json'},
        )
        r.raise_for_status()
        return r.json()


async def get_user_email(access_token: str) -> str:
    """Look up the email of the authenticated user via the userinfo endpoint."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            GOOGLE_USERINFO_URL,
            headers={'Authorization': f'Bearer {access_token}'},
        )
        r.raise_for_status()
        data = r.json()
        if not data.get('email'):
            raise ValueError('userinfo response missing email')
        return data['email']
