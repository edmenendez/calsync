from fastapi.testclient import TestClient

from calsync.main import app


def test_healthz_returns_ok():
    client = TestClient(app)
    response = client.get('/healthz')
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'ok'
    assert 'version' in body
