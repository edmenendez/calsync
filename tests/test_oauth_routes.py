"""Integration-style tests for /oauth/start and /oauth/callback.

Google's token + userinfo endpoints are mocked with respx; no network
traffic. These tests verify the full callback flow end-to-end including
account row creation and refresh-token-clobber behavior.
"""

import respx
from httpx import Response

from calsync.crypto import decrypt_token
from calsync.db import connect
from calsync.oauth import GOOGLE_TOKEN_URL, GOOGLE_USERINFO_URL, make_state


def test_oauth_start_redirects_to_google(app_client, settings):
    r = app_client.get('/oauth/start?account=avela', follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers['location']
    assert loc.startswith('https://accounts.google.com/o/oauth2/v2/auth?')
    assert 'client_id=test-client-id' in loc
    assert 'state=avela' in loc
    assert 'access_type=offline' in loc
    assert 'prompt=consent' in loc


def test_oauth_start_requires_account_param(app_client):
    r = app_client.get('/oauth/start', follow_redirects=False)
    assert r.status_code == 422


def test_oauth_callback_creates_account(app_client, settings):
    state = make_state('avela', settings.admin_token)
    with respx.mock:
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=Response(
                200,
                json={
                    'access_token': 'at-fresh',
                    'refresh_token': 'rt-fresh',
                    'expires_in': 3600,
                    'token_type': 'Bearer',
                    'scope': 'https://www.googleapis.com/auth/calendar',
                },
            )
        )
        respx.get(GOOGLE_USERINFO_URL).mock(
            return_value=Response(200, json={'email': 'ed@avela.org', 'verified_email': True})
        )

        r = app_client.get(f'/oauth/callback?code=auth-code-1&state={state}')

    assert r.status_code == 200
    assert 'avela' in r.text
    assert 'ed@avela.org' in r.text

    with connect(settings.db_path) as conn:
        row = conn.execute('SELECT * FROM accounts WHERE label = ?', ('avela',)).fetchone()
        assert row is not None
        assert row['email'] == 'ed@avela.org'
        assert row['access_token'] == 'at-fresh'
        assert decrypt_token(row['refresh_token_encrypted'], settings.fernet_key) == 'rt-fresh'


def test_oauth_callback_preserves_refresh_token_when_omitted(app_client, settings):
    """If Google omits refresh_token on subsequent auth, we MUST keep the existing one."""
    # First authorization - establishes the refresh token
    state1 = make_state('avela', settings.admin_token)
    with respx.mock:
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=Response(
                200, json={'access_token': 'at-1', 'refresh_token': 'rt-original', 'expires_in': 3600}
            )
        )
        respx.get(GOOGLE_USERINFO_URL).mock(return_value=Response(200, json={'email': 'ed@avela.org'}))
        app_client.get(f'/oauth/callback?code=c1&state={state1}')

    # Second authorization - Google omits refresh_token (real-world Google behavior)
    state2 = make_state('avela', settings.admin_token)
    with respx.mock:
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=Response(
                200,
                json={'access_token': 'at-2', 'expires_in': 3600},  # no refresh_token!
            )
        )
        respx.get(GOOGLE_USERINFO_URL).mock(return_value=Response(200, json={'email': 'ed@avela.org'}))
        r = app_client.get(f'/oauth/callback?code=c2&state={state2}')

    assert r.status_code == 200
    with connect(settings.db_path) as conn:
        row = conn.execute('SELECT * FROM accounts WHERE label = ?', ('avela',)).fetchone()
        assert row['access_token'] == 'at-2'
        assert decrypt_token(row['refresh_token_encrypted'], settings.fernet_key) == 'rt-original'


def test_oauth_callback_rejects_email_change(app_client, settings):
    """Reauth with a different email must NOT silently switch the account."""
    state1 = make_state('avela', settings.admin_token)
    with respx.mock:
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=Response(200, json={'access_token': 'at-1', 'refresh_token': 'rt-1', 'expires_in': 3600})
        )
        respx.get(GOOGLE_USERINFO_URL).mock(return_value=Response(200, json={'email': 'ed@avela.org'}))
        app_client.get(f'/oauth/callback?code=c1&state={state1}')

    state2 = make_state('avela', settings.admin_token)
    with respx.mock:
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=Response(200, json={'access_token': 'at-2', 'refresh_token': 'rt-2', 'expires_in': 3600})
        )
        respx.get(GOOGLE_USERINFO_URL).mock(return_value=Response(200, json={'email': 'someone-else@avela.org'}))
        r = app_client.get(f'/oauth/callback?code=c2&state={state2}')

    assert r.status_code == 409
    assert 'someone-else' in r.text


def test_oauth_callback_rejects_invalid_state(app_client, settings):
    r = app_client.get('/oauth/callback?code=c&state=garbage')
    assert r.status_code == 400
    assert 'state' in r.text.lower()


def test_oauth_callback_rejects_state_signed_with_different_secret(app_client, settings):
    bad = make_state('avela', 'wrong-secret')
    r = app_client.get(f'/oauth/callback?code=c&state={bad}')
    assert r.status_code == 400


def test_oauth_callback_propagates_google_error_param(app_client):
    r = app_client.get('/oauth/callback?error=access_denied')
    assert r.status_code == 400
    assert 'access_denied' in r.text
