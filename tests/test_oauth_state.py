import pytest

from calsync.oauth import build_auth_url, make_state, verify_state


def test_state_roundtrip():
    s = make_state('avela', 'secret')
    assert verify_state(s, 'secret') == 'avela'


def test_state_rejects_wrong_secret():
    s = make_state('avela', 'secret-a')
    with pytest.raises(ValueError, match='invalid state signature'):
        verify_state(s, 'secret-b')


def test_state_rejects_expired():
    s = make_state('avela', 'secret', now=1000)
    with pytest.raises(ValueError, match='expired'):
        verify_state(s, 'secret', now=99999999)


def test_state_rejects_tampered_label():
    s = make_state('avela', 'secret')
    parts = s.split('.', 1)
    tampered = 'beachmedia.' + parts[1]
    with pytest.raises(ValueError, match='invalid state signature'):
        verify_state(tampered, 'secret')


def test_state_rejects_malformed():
    with pytest.raises(ValueError, match='malformed'):
        verify_state('not-a-state', 'secret')


def test_build_auth_url_has_required_params():
    url = build_auth_url(
        account_label='avela',
        client_id='cid',
        redirect_uri='http://localhost:8000/oauth/callback',
        state='ststring',
    )
    assert 'client_id=cid' in url
    assert 'redirect_uri=http' in url
    assert 'response_type=code' in url
    assert 'access_type=offline' in url
    assert 'prompt=consent' in url
    assert 'include_granted_scopes=true' in url
    assert 'state=ststring' in url
    assert 'scope=openid+email' in url
    assert 'auth%2Fcalendar' in url
