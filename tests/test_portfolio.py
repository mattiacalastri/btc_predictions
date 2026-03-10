"""
Unit tests for PortfolioEngine (portfolio_engine.py).

Tests the PortfolioState construction, risk score calculation,
signal evaluation (OPEN/PYRAMID/REVERSE/PARTIAL_CLOSE_AND_OPEN/SKIP),
pyramid sizing, and edge cases. No network calls required.
"""

import os
import sys
import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from portfolio_engine import (
    PortfolioEngine,
    PortfolioState,
    PortfolioDecision,
    MAX_POSITION_BTC,
    PYRAMID_RISK_CEIL,
    PYRAMID_MIN_CONF,
    PYRAMID_MIN_PNL_PCT,
    REVERSE_BASE_THRESHOLD,
    REVERSE_FLOOR,
    PARTIAL_CLOSE_PROFIT_MIN,
    PARTIAL_CLOSE_CONF_MIN,
    FULL_REVERSE_PROFIT_CONF,
    FULL_REVERSE_PROFIT_MIN,
    PARTIAL_LOSS_CONF_MIN,
    PARTIAL_LOSS_PNL_MIN,
    PYRAMID_SIZE_MIN_FACTOR,
    PYRAMID_SIZE_MAX_FACTOR,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _engine():
    return PortfolioEngine()


def _flat_state(**kwargs):
    """Build a FLAT portfolio state with sensible defaults."""
    defaults = dict(equity=100.0, btc_price=80000.0, regime="TRENDING", wr_10=55.0)
    defaults.update(kwargs)
    return _engine().build_state(position=None, **defaults)


def _long_state(size=0.001, price=80000.0, pnl_pct=0.0, pyramid_count=0, **kwargs):
    """Build a LONG portfolio state."""
    defaults = dict(equity=100.0, btc_price=80000.0, regime="TRENDING", wr_10=55.0)
    defaults.update(kwargs)
    return _engine().build_state(
        position={"side": "long", "size": size, "price": price},
        existing_pnl_pct=pnl_pct,
        existing_entry_price=price,
        pyramid_count=pyramid_count,
        **defaults,
    )


def _short_state(size=0.001, price=80000.0, pnl_pct=0.0, pyramid_count=0, **kwargs):
    """Build a SHORT portfolio state."""
    defaults = dict(equity=100.0, btc_price=80000.0, regime="TRENDING", wr_10=55.0)
    defaults.update(kwargs)
    return _engine().build_state(
        position={"side": "short", "size": size, "price": price},
        existing_pnl_pct=pnl_pct,
        existing_entry_price=price,
        pyramid_count=pyramid_count,
        **defaults,
    )


# ── Test PortfolioState dataclass ────────────────────────────────────────────

class TestPortfolioState:
    def test_default_values(self):
        ps = PortfolioState()
        assert ps.net_direction == "FLAT"
        assert ps.total_exposure_btc == 0.0
        assert ps.risk_score == 0.0
        assert ps.positions == []

    def test_to_dict(self):
        ps = PortfolioState(equity=100.0, btc_price=80000.0)
        d = ps.to_dict()
        assert d["equity"] == 100.0
        assert d["btc_price"] == 80000.0
        assert isinstance(d, dict)


class TestPortfolioDecision:
    def test_default_is_skip(self):
        pd = PortfolioDecision()
        assert pd.action == "SKIP"
        assert pd.size == 0.0
        assert pd.is_fallback is False

    def test_to_dict(self):
        pd = PortfolioDecision(action="OPEN", size=0.001, reason="test")
        d = pd.to_dict()
        assert d["action"] == "OPEN"
        assert d["reason"] == "test"


# ── Test build_state ─────────────────────────────────────────────────────────

class TestBuildState:
    def test_flat_position(self):
        state = _flat_state()
        assert state.net_direction == "FLAT"
        assert state.total_exposure_btc == 0.0
        assert state.positions == []
        assert state.risk_score == 0.0

    def test_long_position(self):
        state = _long_state(size=0.001, price=79000.0, pnl_pct=0.005)
        assert state.net_direction == "LONG"
        assert state.total_exposure_btc == 0.001
        assert state.unrealized_pnl_pct == 0.005
        assert len(state.positions) == 1
        assert state.positions[0]["side"] == "long"

    def test_short_position(self):
        state = _short_state(size=0.002, price=81000.0, pnl_pct=0.003)
        assert state.net_direction == "SHORT"
        assert state.total_exposure_btc == 0.002

    def test_exposure_pct_calculated(self):
        state = _long_state(size=0.001, btc_price=80000.0, equity=100.0)
        # 0.001 * 80000 / 100 * 100 = 80%
        assert state.total_exposure_pct == 80.0

    def test_unrealized_pnl_usd_long(self):
        engine = _engine()
        state = engine.build_state(
            position={"side": "long", "size": 0.001, "price": 79000.0},
            equity=100.0, btc_price=80000.0, existing_pnl_pct=0.01,
        )
        # (80000 - 79000) * 0.001 * 1 = 1.0
        assert state.unrealized_pnl_usd == pytest.approx(1.0, abs=0.01)

    def test_unrealized_pnl_usd_short(self):
        engine = _engine()
        state = engine.build_state(
            position={"side": "short", "size": 0.001, "price": 81000.0},
            equity=100.0, btc_price=80000.0, existing_pnl_pct=0.01,
        )
        # (80000 - 81000) * 0.001 * -1 = 1.0
        assert state.unrealized_pnl_usd == pytest.approx(1.0, abs=0.01)

    def test_unknown_side_is_flat(self):
        engine = _engine()
        state = engine.build_state(
            position={"side": "unknown", "size": 0.001, "price": 80000.0},
            equity=100.0, btc_price=80000.0,
        )
        assert state.net_direction == "FLAT"

    def test_none_position_is_flat(self):
        state = _flat_state()
        assert state.net_direction == "FLAT"

    def test_zero_equity_no_crash(self):
        state = _long_state(equity=0.0)
        assert state.total_exposure_pct == 0.0

    def test_zero_price_no_crash(self):
        engine = _engine()
        state = engine.build_state(
            position={"side": "long", "size": 0.001, "price": 0},
            equity=100.0, btc_price=0,
        )
        assert state.unrealized_pnl_usd == 0.0


# ── Test risk score calculation ──────────────────────────────────────────────

class TestRiskScore:
    def test_flat_is_zero_risk(self):
        state = _flat_state()
        assert state.risk_score == 0.0

    def test_high_exposure_increases_risk(self):
        state = _long_state(size=0.003, btc_price=80000.0, equity=100.0)
        # Exposure: 0.003 * 80000 / 100 * 100 = 240%. Score += 240 * 0.1 = 24.0
        assert state.risk_score >= 20.0

    def test_unrealized_loss_increases_risk(self):
        state = _long_state(size=0.001, pnl_pct=-0.02)
        # Loss: abs(-0.02) * 100 * 15 = 30.0
        assert state.risk_score >= 30.0

    def test_losing_streak_increases_risk(self):
        state = _long_state(streak_count=4, streak_direction="loss")
        # 4 * 5 = 20 pts from streak
        assert state.risk_score >= 15.0

    def test_low_wr_increases_risk(self):
        state = _long_state(wr_10=35.0)
        # wr < 40 → +10 pts
        assert state.risk_score >= 10.0

    def test_risk_score_capped_at_100(self):
        state = _long_state(
            size=0.005, pnl_pct=-0.03,
            streak_count=6, streak_direction="loss",
            wr_10=30.0, equity=10.0, btc_price=80000.0,
        )
        assert state.risk_score <= 100.0


# ── Test evaluate_signal: FLAT → OPEN ────────────────────────────────────────

class TestEvaluateFlat:
    def test_flat_always_opens(self):
        engine = _engine()
        state = _flat_state()
        decision = engine.evaluate_signal(state, "UP", 0.70, 0.60, 0.001)
        assert decision.action == "OPEN"
        assert decision.size == 0.001
        assert decision.reason == "flat_new_position"

    def test_flat_open_down(self):
        engine = _engine()
        state = _flat_state()
        decision = engine.evaluate_signal(state, "DOWN", 0.65, 0.40, 0.002)
        assert decision.action == "OPEN"
        assert decision.size == 0.002


# ── Test evaluate_signal: SAME direction (pyramid) ──────────────────────────

class TestEvaluateSameDirection:
    def test_pyramid_all_gates_pass(self):
        engine = _engine()
        state = _long_state(size=0.001, pnl_pct=0.005, pyramid_count=0)
        state.risk_score = 10.0  # below PYRAMID_RISK_CEIL
        decision = engine.evaluate_signal(state, "UP", 0.70, 0.60, 0.001)
        assert decision.action == "PYRAMID"
        assert decision.size > 0

    def test_pyramid_blocked_if_already_done(self):
        engine = _engine()
        state = _long_state(size=0.001, pnl_pct=0.005, pyramid_count=1)
        decision = engine.evaluate_signal(state, "UP", 0.70, 0.60, 0.001)
        assert decision.action == "SKIP"
        assert "pyramid_already_done" in decision.reason

    def test_pyramid_blocked_if_position_too_large(self):
        engine = _engine()
        state = _long_state(size=MAX_POSITION_BTC, pnl_pct=0.005, pyramid_count=0)
        decision = engine.evaluate_signal(state, "UP", 0.80, 0.75, 0.001)
        assert decision.action == "SKIP"
        assert "position_too_large" in decision.reason

    def test_pyramid_blocked_by_high_risk(self):
        engine = _engine()
        state = _long_state(size=0.001, pnl_pct=0.005, pyramid_count=0)
        state.risk_score = PYRAMID_RISK_CEIL + 1
        decision = engine.evaluate_signal(state, "UP", 0.70, 0.60, 0.001)
        assert decision.action == "SKIP"
        assert "risk_too_high" in decision.reason

    def test_pyramid_blocked_by_low_confidence(self):
        engine = _engine()
        state = _long_state(size=0.001, pnl_pct=0.005, pyramid_count=0)
        state.risk_score = 10.0
        decision = engine.evaluate_signal(state, "UP", PYRAMID_MIN_CONF - 0.01, 0.50, 0.001)
        assert decision.action == "SKIP"
        assert "confidence_too_low" in decision.reason

    def test_pyramid_blocked_if_in_loss(self):
        engine = _engine()
        state = _long_state(size=0.001, pnl_pct=-0.002, pyramid_count=0)
        state.risk_score = 10.0
        decision = engine.evaluate_signal(state, "UP", 0.80, 0.75, 0.001)
        assert decision.action == "SKIP"
        assert "loss_position_no_pyramid" in decision.reason

    def test_pyramid_strong_xgb_bypass(self):
        engine = _engine()
        # pnl below PYRAMID_MIN_PNL_PCT but strong XGB
        state = _long_state(size=0.001, pnl_pct=0.0001, pyramid_count=0)
        state.risk_score = 10.0
        decision = engine.evaluate_signal(state, "UP", 0.75, 0.75, 0.001)
        assert decision.action == "PYRAMID"
        assert "strong_xgb_bypass" in decision.reason

    def test_pyramid_skip_low_pnl_no_xgb(self):
        engine = _engine()
        state = _long_state(size=0.001, pnl_pct=0.0001, pyramid_count=0)
        state.risk_score = 10.0
        # Low xgb directional and low confidence (but above PYRAMID_MIN_CONF)
        decision = engine.evaluate_signal(state, "UP", 0.66, 0.55, 0.001)
        assert decision.action == "SKIP"
        assert "pnl_too_low" in decision.reason


# ── Test evaluate_signal: OPPOSITE direction ─────────────────────────────────

class TestEvaluateOppositeDirection:
    def test_profit_partial_close_and_open(self):
        engine = _engine()
        # Long with profit >= PARTIAL_CLOSE_PROFIT_MIN, conf >= PARTIAL_CLOSE_CONF_MIN
        state = _long_state(size=0.002, pnl_pct=PARTIAL_CLOSE_PROFIT_MIN + 0.001)
        decision = engine.evaluate_signal(state, "DOWN", PARTIAL_CLOSE_CONF_MIN + 0.01, 0.40, 0.001)
        assert decision.action == "PARTIAL_CLOSE_AND_OPEN"
        assert decision.close_size == pytest.approx(0.001)  # 50% of 0.002
        assert decision.size > 0
        assert "profit_partial_close" in decision.reason

    def test_profit_full_reverse(self):
        engine = _engine()
        state = _long_state(size=0.002, pnl_pct=FULL_REVERSE_PROFIT_MIN + 0.001)
        # High confidence, but pnl below PARTIAL_CLOSE_PROFIT_MIN so partial is skipped
        # Actually: pnl=0.006 < PARTIAL_CLOSE_PROFIT_MIN=0.01, so partial not triggered
        decision = engine.evaluate_signal(state, "DOWN", FULL_REVERSE_PROFIT_CONF + 0.01, 0.40, 0.001)
        assert decision.action == "REVERSE"
        assert decision.close_size == 0.002
        assert "profit_full_reverse" in decision.reason

    def test_profit_preserve_when_low_confidence(self):
        engine = _engine()
        state = _long_state(size=0.002, pnl_pct=0.003)
        decision = engine.evaluate_signal(state, "DOWN", 0.55, 0.40, 0.001)
        assert decision.action == "SKIP"
        assert "preserve_profit" in decision.reason

    def test_loss_reverse_high_confidence(self):
        engine = _engine()
        state = _long_state(size=0.002, pnl_pct=-0.005)
        # Dynamic threshold: max(REVERSE_FLOOR, 0.75 - 0.5*0.1) = max(0.55, 0.70) = 0.70
        decision = engine.evaluate_signal(state, "DOWN", 0.75, 0.30, 0.001)
        assert decision.action == "REVERSE"
        assert "loss_reverse" in decision.reason

    def test_loss_partial_close(self):
        engine = _engine()
        state = _long_state(size=0.002, pnl_pct=-0.008)
        # Loss 0.8%, threshold = max(0.55, 0.75 - 0.8*0.1) = max(0.55, 0.67) = 0.67
        # Confidence below threshold but above PARTIAL_LOSS_CONF_MIN
        decision = engine.evaluate_signal(state, "DOWN", 0.66, 0.30, 0.001)
        assert decision.action == "PARTIAL_CLOSE_AND_OPEN"
        assert "loss_partial_close" in decision.reason

    def test_loss_skip_when_low_confidence(self):
        engine = _engine()
        state = _long_state(size=0.002, pnl_pct=-0.002)
        decision = engine.evaluate_signal(state, "DOWN", 0.55, 0.30, 0.001)
        assert decision.action == "SKIP"
        assert "opposite_skip" in decision.reason

    def test_dynamic_reverse_threshold_scales_with_loss(self):
        engine = _engine()
        # Big loss: -2% → threshold = max(0.55, 0.75 - 2.0*0.1) = max(0.55, 0.55) = 0.55
        state = _long_state(size=0.002, pnl_pct=-0.02)
        decision = engine.evaluate_signal(state, "DOWN", 0.56, 0.30, 0.001)
        assert decision.action == "REVERSE"

    def test_short_opposite_is_up(self):
        engine = _engine()
        state = _short_state(size=0.002, pnl_pct=-0.005)
        decision = engine.evaluate_signal(state, "UP", 0.80, 0.70, 0.001)
        assert decision.action == "REVERSE"


# ── Test pyramid sizing ──────────────────────────────────────────────────────

class TestPyramidSizing:
    def test_min_size_at_low_conf_zero_profit(self):
        engine = _engine()
        size = engine._calculate_pyramid_size(0.001, PYRAMID_MIN_CONF, 0.0)
        # conf_factor=0, profit_factor=0 → combined=0 → pyr_pct=0.30
        expected = max(0.001, round(0.001 * PYRAMID_SIZE_MIN_FACTOR, 6))
        assert size == pytest.approx(expected, abs=0.0001)

    def test_max_size_at_high_conf_high_profit(self):
        engine = _engine()
        # Use a larger base so result exceeds Kraken minimum of 0.001
        size = engine._calculate_pyramid_size(0.003, 1.0, 0.01)
        # conf_factor=1, profit_factor=1 → combined=1 → pyr_pct=0.75
        expected = round(0.003 * PYRAMID_SIZE_MAX_FACTOR, 6)
        assert size == pytest.approx(expected, abs=0.0001)

    def test_minimum_kraken_size(self):
        engine = _engine()
        # Very small base → should floor at 0.001
        size = engine._calculate_pyramid_size(0.0001, PYRAMID_MIN_CONF, 0.0)
        assert size >= 0.001


# ── Test project_risk ────────────────────────────────────────────────────────

class TestProjectRisk:
    def test_risk_after_new_position(self):
        engine = _engine()
        state = _flat_state(equity=100.0, btc_price=80000.0)
        risk = engine._project_risk(state, 0.001, "UP")
        # 0.001 * 80000 / 100 * 100 = 80% → 80 * 0.1 = 8.0
        assert risk == pytest.approx(8.0, abs=0.5)

    def test_risk_capped_at_100(self):
        engine = _engine()
        state = _long_state(
            size=0.005, pnl_pct=-0.03,
            streak_count=6, streak_direction="loss",
            wr_10=30.0, equity=10.0,
        )
        risk = engine._project_risk(state, 0.005, "UP")
        assert risk <= 100.0


# ── Test kill switch ─────────────────────────────────────────────────────────

class TestKillSwitch:
    def test_disabled_flag(self, monkeypatch):
        monkeypatch.setenv("PORTFOLIO_ENGINE_DISABLED", "true")
        engine = PortfolioEngine()
        assert engine.disabled is True

    def test_not_disabled_by_default(self):
        engine = PortfolioEngine()
        assert engine.disabled is False
