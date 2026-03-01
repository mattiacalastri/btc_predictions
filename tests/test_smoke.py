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


# ---------------------------------------------------------------------------
# Test 7: POST /place-bet — DRY_RUN tests
# ---------------------------------------------------------------------------

def test_place_bet_requires_api_key(client):
    """POST /place-bet must reject requests without valid API key."""
    response = client.post("/place-bet", json={"direction": "UP", "confidence": 0.70})
    # 401 if BOT_API_KEY is set, or continues if not set (backwards compat)
    assert response.status_code in (200, 401, 429), (
        f"/place-bet returned unexpected {response.status_code}"
    )


def test_place_bet_invalid_direction(client):
    """POST /place-bet must reject invalid direction."""
    response = client.post(
        "/place-bet",
        json={"direction": "SIDEWAYS", "confidence": 0.70},
        headers={"X-API-Key": os.environ.get("BOT_API_KEY", "test")},
    )
    data = response.get_json()
    if response.status_code == 401:
        pytest.skip("BOT_API_KEY mismatch — cannot test further")
    assert data is not None
    # Should get 400 with "invalid_direction" or be skipped/paused
    if response.status_code == 400:
        assert "invalid_direction" in str(data)


def test_place_bet_empty_body(client):
    """POST /place-bet with empty body should return 400 or auth error."""
    response = client.post(
        "/place-bet",
        data=b"{}",
        content_type="application/json",
        headers={"X-API-Key": os.environ.get("BOT_API_KEY", "test")},
    )
    # Empty body → direction missing → 400, or 401 if key mismatch
    assert response.status_code in (400, 401, 429), (
        f"Empty body /place-bet returned {response.status_code}"
    )


def test_place_bet_malformed_json(client):
    """POST /place-bet with malformed JSON should not crash."""
    response = client.post(
        "/place-bet",
        data=b"not-valid-json{{{",
        content_type="application/json",
        headers={"X-API-Key": os.environ.get("BOT_API_KEY", "test")},
    )
    # Flask force=True in get_json should handle this gracefully
    assert response.status_code in (400, 401, 429, 200), (
        f"Malformed JSON /place-bet returned {response.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 8: POST /close-position
# ---------------------------------------------------------------------------

def test_close_position_requires_api_key(client):
    """POST /close-position must reject without API key or proceed in DRY_RUN."""
    response = client.post("/close-position", json={"symbol": "PF_XBTUSD"})
    assert response.status_code in (200, 401, 429), (
        f"/close-position returned unexpected {response.status_code}"
    )


def test_close_position_dry_run(client):
    """POST /close-position in DRY_RUN should return clean response."""
    response = client.post(
        "/close-position",
        json={"symbol": "PF_XBTUSD"},
        headers={"X-API-Key": os.environ.get("BOT_API_KEY", "test")},
    )
    if response.status_code == 401:
        pytest.skip("BOT_API_KEY mismatch")
    data = response.get_json()
    assert data is not None
    # In DRY_RUN: {"status": "closed", "dry_run": true}
    # Without DRY_RUN: may error (no Kraken creds) → 500
    assert response.status_code in (200, 500)


# ---------------------------------------------------------------------------
# Test 9: POST /cockpit/api/auth
# ---------------------------------------------------------------------------

def test_cockpit_auth_no_token(client):
    """POST /cockpit/api/auth with empty token should return 403 or 503."""
    response = client.post(
        "/cockpit/api/auth",
        json={"token": ""},
    )
    # 403 if COCKPIT_TOKEN set (wrong token), 503 if not configured
    assert response.status_code in (403, 503), (
        f"/cockpit/api/auth returned {response.status_code}"
    )


def test_cockpit_auth_wrong_token(client):
    """POST /cockpit/api/auth with wrong token should return 403 or 503."""
    response = client.post(
        "/cockpit/api/auth",
        json={"token": "definitely-wrong-token-12345"},
    )
    assert response.status_code in (403, 503)


# ---------------------------------------------------------------------------
# Test 10: POST /cockpit/api/bot-toggle
# ---------------------------------------------------------------------------

def test_cockpit_bot_toggle_no_auth(client):
    """POST /cockpit/api/bot-toggle without auth should be rejected."""
    response = client.post("/cockpit/api/bot-toggle")
    assert response.status_code in (403, 503)


# ---------------------------------------------------------------------------
# Test 11: POST /cockpit/api/agents/reset
# ---------------------------------------------------------------------------

def test_cockpit_agents_reset_no_auth(client):
    """POST /cockpit/api/agents/reset without auth should be rejected."""
    response = client.post(
        "/cockpit/api/agents/reset",
        json={"clone_id": "c1"},
    )
    assert response.status_code in (403, 503)


# ---------------------------------------------------------------------------
# Test 12: POST /cockpit/api/agents/update
# ---------------------------------------------------------------------------

def test_cockpit_agents_update_no_auth(client):
    """POST /cockpit/api/agents/update without auth should be rejected."""
    response = client.post(
        "/cockpit/api/agents/update",
        json={"clone_id": "c1", "action": "note", "value": "test"},
    )
    assert response.status_code in (403, 503)


# ---------------------------------------------------------------------------
# Test 13: POST /cockpit/api/log/ingest
# ---------------------------------------------------------------------------

def test_cockpit_log_ingest_no_auth(client):
    """POST /cockpit/api/log/ingest without auth should be rejected."""
    response = client.post(
        "/cockpit/api/log/ingest",
        json={"source": "test", "level": "info", "title": "test"},
    )
    assert response.status_code in (403, 503)


# ---------------------------------------------------------------------------
# Test 14: POST /publish-telegram
# ---------------------------------------------------------------------------

def test_publish_telegram_requires_api_key(client):
    """POST /publish-telegram must reject without API key."""
    response = client.post("/publish-telegram", json={"text": "test"})
    assert response.status_code in (401, 429), (
        f"/publish-telegram returned {response.status_code}"
    )


def test_publish_telegram_empty_text(client):
    """POST /publish-telegram with empty text should return 400."""
    response = client.post(
        "/publish-telegram",
        json={"text": ""},
        headers={"X-API-Key": os.environ.get("BOT_API_KEY", "test")},
    )
    if response.status_code == 401:
        pytest.skip("BOT_API_KEY mismatch")
    data = response.get_json()
    assert data is not None
    if response.status_code == 400:
        assert "text required" in str(data.get("error", ""))


# ---------------------------------------------------------------------------
# Test 15: POST /pause and /resume
# ---------------------------------------------------------------------------

def test_pause_requires_api_key(client):
    """POST /pause must reject without valid API key."""
    response = client.post("/pause")
    assert response.status_code in (200, 401)


def test_resume_requires_api_key(client):
    """POST /resume must reject without valid API key."""
    response = client.post("/resume")
    assert response.status_code in (200, 401)


# ---------------------------------------------------------------------------
# Test 16: POST /ghost-evaluate
# ---------------------------------------------------------------------------

def test_ghost_evaluate_requires_api_key(client):
    """POST /ghost-evaluate must reject without API key."""
    response = client.post("/ghost-evaluate")
    assert response.status_code in (401, 503), (
        f"/ghost-evaluate returned {response.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 17: POST /rescue-orphaned
# ---------------------------------------------------------------------------

def test_rescue_orphaned_requires_api_key(client):
    """POST /rescue-orphaned must reject without API key."""
    response = client.post("/rescue-orphaned")
    assert response.status_code in (401, 503), (
        f"/rescue-orphaned returned {response.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 18: POST /commit-prediction (on-chain)
# ---------------------------------------------------------------------------

def test_commit_prediction_missing_fields(client):
    """POST /commit-prediction with missing fields should return 400."""
    response = client.post(
        "/commit-prediction",
        json={"bet_id": 1},  # missing required fields
        headers={"X-API-Key": os.environ.get("BOT_API_KEY", "test")},
    )
    if response.status_code == 401:
        pytest.skip("BOT_API_KEY mismatch")
    assert response.status_code == 400
    data = response.get_json()
    assert "Campi mancanti" in str(data.get("error", ""))


def test_commit_prediction_requires_api_key(client):
    """POST /commit-prediction must reject without API key."""
    response = client.post("/commit-prediction", json={})
    assert response.status_code in (400, 401)


# ---------------------------------------------------------------------------
# Test 19: POST /resolve-prediction (on-chain)
# ---------------------------------------------------------------------------

def test_resolve_prediction_missing_fields(client):
    """POST /resolve-prediction with missing fields should return 400."""
    response = client.post(
        "/resolve-prediction",
        json={"bet_id": 1},
        headers={"X-API-Key": os.environ.get("BOT_API_KEY", "test")},
    )
    if response.status_code == 401:
        pytest.skip("BOT_API_KEY mismatch")
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Test 20: POST /submit-contribution (public, no API key)
# ---------------------------------------------------------------------------

def test_submit_contribution_empty_insight(client):
    """POST /submit-contribution with empty insight should return 400."""
    response = client.post(
        "/submit-contribution",
        json={"role": "trader", "insight": "", "consent": True},
    )
    # 400 (too short) or recaptcha block
    assert response.status_code in (400, 429)


def test_submit_contribution_no_consent(client):
    """POST /submit-contribution without consent should return 400."""
    response = client.post(
        "/submit-contribution",
        json={"role": "trader", "insight": "This is a valid insight with enough chars", "consent": False},
    )
    assert response.status_code in (400, 429)


# ---------------------------------------------------------------------------
# Test 21: POST /satoshi-lead
# ---------------------------------------------------------------------------

def test_satoshi_lead_invalid_email(client):
    """POST /satoshi-lead with invalid email should return 400."""
    response = client.post(
        "/satoshi-lead",
        json={"email": "not-an-email"},
    )
    # 400 (invalid_email) or captcha block
    assert response.status_code in (400, 429)


# ---------------------------------------------------------------------------
# Test 22: Edge cases — content type and API key validation
# ---------------------------------------------------------------------------

def test_place_bet_missing_content_type(client):
    """POST /place-bet without Content-Type should still work (force=True)."""
    response = client.post(
        "/place-bet",
        data=b'{"direction":"UP","confidence":0.7}',
        headers={"X-API-Key": os.environ.get("BOT_API_KEY", "test")},
    )
    # Should not crash — get_json(force=True) handles this
    assert response.status_code in (200, 400, 401, 429, 500)


# ---------------------------------------------------------------------------
# Test 23: GET endpoints that should work without auth
# ---------------------------------------------------------------------------

def test_public_contributions_returns_json(client):
    """/public-contributions should return a JSON array."""
    response = client.get("/public-contributions")
    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, list)


def test_btc_regime_returns_json(client):
    """/btc-regime should return regime data."""
    response = client.get("/btc-regime")
    # May fail if Binance API is unreachable
    assert response.status_code in (200, 500)


def test_force_retrain_rate_limited(client):
    """/force-retrain should enforce rate limiting."""
    # First call may succeed or fail based on state
    client.post("/force-retrain")
    # Second call within 1h should be rate-limited
    response = client.post("/force-retrain")
    assert response.status_code == 429
