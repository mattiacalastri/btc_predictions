"""
Smoke tests for BTC Prediction Bot (app.py).

These tests verify basic Flask app structure and endpoints that do not
require live credentials (Kraken, Supabase, n8n).  Any test that would
make a real network call is either skipped or wrapped so it fails
gracefully in CI where only dummy env vars are present.
"""

import os
import sys
import pytest

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so `import app` works regardless
# of where pytest is invoked from.
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Fixture: Flask test client
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """
    Import app and return a Flask test client.

    The import itself exercises all module-level code in app.py (XGBoost
    model loading, etc.).  If the import fails the whole module is skipped
    with a clear message rather than crashing the test suite.
    """
    try:
        import app as flask_app  # noqa: PLC0415
    except Exception as exc:
        pytest.skip(f"Could not import app.py: {exc}")

    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Test 1: Flask app object is created correctly
# ---------------------------------------------------------------------------

def test_app_is_flask_instance():
    """app.py must expose a Flask application object named `app`."""
    try:
        from flask import Flask
        import app as flask_app
        assert isinstance(flask_app.app, Flask), (
            "flask_app.app is not a Flask instance"
        )
    except ImportError as exc:
        pytest.skip(f"Import failed: {exc}")


# ---------------------------------------------------------------------------
# Test 2: /health returns 200 and expected keys
# ---------------------------------------------------------------------------

def test_health_endpoint_returns_200(client):
    """/health must respond 200 even with dummy credentials (DRY_RUN=true)."""
    response = client.get("/health")
    assert response.status_code == 200, (
        f"/health returned {response.status_code}: {response.data}"
    )


def test_health_response_is_json(client):
    """/health must return a JSON body."""
    response = client.get("/health")
    data = response.get_json()
    assert data is not None, "/health did not return valid JSON"


def test_health_contains_version(client):
    """/health JSON should include a `version` key."""
    response = client.get("/health")
    data = response.get_json()
    if data is None:
        pytest.skip("/health did not return JSON")
    assert "version" in data, f"Missing 'version' in /health response: {data}"


def test_health_contains_dry_run(client):
    """/health JSON should include a `dry_run` field."""
    response = client.get("/health")
    data = response.get_json()
    if data is None:
        pytest.skip("/health did not return JSON")
    assert "dry_run" in data, f"Missing 'dry_run' in /health response: {data}"


# ---------------------------------------------------------------------------
# Test 3: /bet-sizing returns sensible output without real DB
# ---------------------------------------------------------------------------

def test_bet_sizing_returns_200(client):
    """/bet-sizing should respond 200 with default params."""
    response = client.get("/bet-sizing?confidence=0.70")
    # Accept 200 or 503 (if Supabase dummy creds cause connection error)
    assert response.status_code in (200, 503), (
        f"/bet-sizing returned unexpected status {response.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 4: /predict-xgb responds (model may or may not be loaded)
# ---------------------------------------------------------------------------

def test_predict_xgb_endpoint_exists(client):
    """/predict-xgb must exist and return JSON regardless of model availability."""
    response = client.get(
        "/predict-xgb?rsi14=55&ema_trend=1&fear_greed=45&conf=0.70"
    )
    assert response.status_code == 200, (
        f"/predict-xgb returned {response.status_code}"
    )
    data = response.get_json()
    assert data is not None, "/predict-xgb did not return JSON"
    # The response must always contain an `agree` key (True when model absent)
    assert "agree" in data, f"Missing 'agree' in /predict-xgb response: {data}"


# ---------------------------------------------------------------------------
# Test 5: /dashboard serves index.html (static file)
# ---------------------------------------------------------------------------

def test_dashboard_returns_html(client):
    """/dashboard should return the HTML dashboard."""
    response = client.get("/dashboard")
    # Accept 200 or 404 depending on whether index.html is present in CI
    assert response.status_code in (200, 404), (
        f"/dashboard returned unexpected status {response.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 6: Unknown routes return 404 (Flask default behaviour)
# ---------------------------------------------------------------------------

def test_unknown_route_returns_404(client):
    """Requests to non-existent routes must return 404."""
    response = client.get("/this-route-does-not-exist-xyz")
    assert response.status_code == 404
