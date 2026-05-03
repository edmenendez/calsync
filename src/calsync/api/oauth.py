"""OAuth start + callback endpoints."""

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from calsync.config import Settings
from calsync.crypto import encrypt_token
from calsync.db import connect
from calsync.deps import get_settings
from calsync.oauth import (
    build_auth_url,
    exchange_code,
    get_user_email,
    make_state,
    verify_state,
)
from calsync.repositories.accounts import AccountAuthMismatchError, upsert_oauth_credentials

router = APIRouter(prefix='/oauth')

SettingsDep = Annotated[Settings, Depends(get_settings)]


def _redirect_uri(settings: Settings) -> str:
    return f'{settings.public_url.rstrip("/")}/oauth/callback'


@router.get('/start')
async def oauth_start(settings: SettingsDep, account: Annotated[str, Query(min_length=1)]):
    state = make_state(account, settings.admin_token)
    url = build_auth_url(
        account_label=account,
        client_id=settings.google_client_id,
        redirect_uri=_redirect_uri(settings),
        state=state,
    )
    return RedirectResponse(url=url, status_code=302)


@router.get('/callback', response_class=HTMLResponse)
async def oauth_callback(
    settings: SettingsDep,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
):
    if error:
        raise HTTPException(status_code=400, detail=f'OAuth error: {error}')
    if not code or not state:
        raise HTTPException(status_code=400, detail='missing code or state')

    try:
        account_label = verify_state(state, settings.admin_token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f'state invalid: {e}') from e

    tokens = await exchange_code(
        code=code,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        redirect_uri=_redirect_uri(settings),
    )
    access_token = tokens['access_token']
    refresh_token = tokens.get('refresh_token')
    expires_in = tokens.get('expires_in', 3600)
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=int(expires_in))

    email = await get_user_email(access_token)

    encrypted_rt = encrypt_token(refresh_token, settings.fernet_key) if refresh_token else None

    with connect(settings.db_path) as conn:
        try:
            upsert_oauth_credentials(
                conn,
                label=account_label,
                email=email,
                encrypted_refresh_token=encrypted_rt,
                access_token=access_token,
                access_token_expires_at=expires_at,
            )
        except AccountAuthMismatchError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    return HTMLResponse(
        f"""<!doctype html>
<html><body style="font-family: system-ui; padding: 2em;">
<h2>Account &lsquo;{account_label}&rsquo; authorized</h2>
<p>Authenticated as <code>{email}</code>.</p>
</body></html>""",
        status_code=200,
    )
