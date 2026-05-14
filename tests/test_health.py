"""
Smoke test for the M4 scaffold: the app boots and the liveness probe answers.
"""

from fastapi.testclient import TestClient


def test_health_ok(client: TestClient) -> None:
    """GET /health returns 200 with the expected liveness body."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
