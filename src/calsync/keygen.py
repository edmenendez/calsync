"""Generate fresh values for the three required secrets.

Run with:

    uv run python -m calsync.keygen

Outputs the three keys in .env-pasteable format. Run once at first
install, then store the resulting values in your .env (dev) or systemd
EnvironmentFile (prod). Losing the HMAC key forces a full mirror
wipe-and-rebuild on recovery; back it up alongside the SQLite DB.
"""

import secrets

from calsync.crypto import generate_fernet_key, generate_hmac_key


def main() -> None:
    print('# calsync secret keys - generated', '\n')
    print(f'CALSYNC_FERNET_KEY={generate_fernet_key()}')
    print(f'CALSYNC_MIRROR_HMAC_KEY={generate_hmac_key()}')
    print(f'CALSYNC_ADMIN_TOKEN={secrets.token_urlsafe(32)}')


if __name__ == '__main__':
    main()
