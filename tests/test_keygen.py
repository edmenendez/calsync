import io
from contextlib import redirect_stdout

from calsync.keygen import main


def test_keygen_emits_three_env_lines():
    buf = io.StringIO()
    with redirect_stdout(buf):
        main()
    out = buf.getvalue()

    assert 'CALSYNC_FERNET_KEY=' in out
    assert 'CALSYNC_MIRROR_HMAC_KEY=' in out
    assert 'CALSYNC_ADMIN_TOKEN=' in out


def test_keygen_produces_distinct_values_per_run():
    a = io.StringIO()
    b = io.StringIO()
    with redirect_stdout(a):
        main()
    with redirect_stdout(b):
        main()
    assert a.getvalue() != b.getvalue()
