"""Tests for GET /health endpoint.

Exact contract: HTTP 200, body = {"status": "ok"}.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


class TestHealthEndpoint:
    def test_returns_200(self) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_returns_ok_status(self) -> None:
        resp = client.get("/health")
        assert resp.json() == {"status": "ok"}

    def test_no_extra_fields(self) -> None:
        resp = client.get("/health")
        body = resp.json()
        assert set(body.keys()) == {"status"}