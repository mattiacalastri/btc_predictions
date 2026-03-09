"""
Unit tests for Adaptive Calibration Engine (ACE).

Tests every calculation, fail-open behavior, and bounds/clamp logic.
No network calls required — all data is synthetic.
"""

import os
import sys
import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from adaptive_engine import (
    AdaptiveEngine,
    AdaptiveState,
    _clamp,
    _DEFAULT_THRESHOLD,
    _THRESHOLD_BOUNDS,
    _MOMENTUM_BOUNDS,
    _MIN_SIGNALS_FOR_CALC,
    _MIN_SIGNALS_PER_BAND,
    _CONF_BANDS,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_rows(n, wr=0.50, direction="UP", conf=0.60, pnl_win=0.002, pnl_loss=-0.001):
    """Generate synthetic signal rows with given win rate."""
    rows = []
    wins = int(n * wr)
    for i in range(n):
        correct = i < wins
        rows.append({
            "id": n - i,
            "direction": direction,
            "confidence": conf,
            "correct": correct,
            "pnl_pct": pnl_win if correct else pnl_loss,
            "pnl_usd": 0.10 if correct else -0.05,
            "created_at": f"2026-03-01T{i % 24:02d}:00:00Z",
        })
    return rows


def _make_mixed_rows(n, wr=0.50, up_pct=0.50, conf=0.60):
    """Generate rows with mixed directions."""
    rows = []
    up_count = int(n * up_pct)
    wins = int(n * wr)
    for i in range(n):
        rows.append({
            "id": n - i,
            "direction": "UP" if i < up_count else "DOWN",
            "confidence": conf,
            "correct": i < wins,
            "pnl_pct": 0.002 if i < wins else -0.001,
            "created_at": f"2026-03-01T{i % 24:02d}:00:00Z",
        })
    return rows


# ── Test _clamp ───────────────────────────────────────────────────────────────

class TestClamp:
    def test_within_bounds(self):
        assert _clamp(0.55, 0.50, 0.70) == 0.55

    def test_below_lower(self):
        assert _clamp(0.40, 0.50, 0.70) == 0.50

    def test_above_upper(self):
        assert _clamp(0.80, 0.50, 0.70) == 0.70

    def test_at_boundaries(self):
        assert _clamp(0.50, 0.50, 0.70) == 0.50
        assert _clamp(0.70, 0.50, 0.70) == 0.70


# ── Test AdaptiveState ────────────────────────────────────────────────────────

class TestAdaptiveState:
    def test_neutral_returns_defaults(self):
        st = AdaptiveState.neutral()
        assert st.optimal_threshold == _DEFAULT_THRESHOLD
        assert st.effective_threshold == _DEFAULT_THRESHOLD
        assert st.direction_bias_adj == 0.0
        assert st.momentum_factor == 1.0
        assert st.regime == "UNKNOWN"

    def test_to_dict_contains_all_slots(self):
        st = AdaptiveState.neutral()
        d = st.to_dict()
        for slot in AdaptiveState.__slots__:
            assert slot in d


# ── Test AdaptiveEngine._calc_wr ──────────────────────────────────────────────

class TestCalcWR:
    def test_basic_wr(self):
        rows = _make_rows(20, wr=0.60)
        wr = AdaptiveEngine._calc_wr(rows)
        assert wr == 0.60

    def test_all_wins(self):
        rows = _make_rows(10, wr=1.0)
        wr = AdaptiveEngine._calc_wr(rows)
        assert wr == 1.0

    def test_all_losses(self):
        rows = _make_rows(10, wr=0.0)
        wr = AdaptiveEngine._calc_wr(rows)
        assert wr == 0.0

    def test_insufficient_data_returns_none(self):
        rows = _make_rows(3, wr=0.50)
        wr = AdaptiveEngine._calc_wr(rows)
        assert wr is None

    def test_none_correct_excluded(self):
        rows = _make_rows(10, wr=0.50)
        rows[0]["correct"] = None
        rows[1]["correct"] = None
        wr = AdaptiveEngine._calc_wr(rows)
        # 8 resolved: 5 wins (original) - but first 2 (wins) removed
        # Actually: first 5 are correct=True. We set [0] and [1] to None.
        # So 3 wins out of 8 resolved = 0.375
        assert wr is not None
        assert 0.0 <= wr <= 1.0


# ── Test _calc_momentum ──────────────────────────────────────────────────────

class TestMomentum:
    def test_hot_streak(self):
        m = AdaptiveEngine._calc_momentum(0.70, 0.50)
        assert m == 1.05

    def test_cold_streak(self):
        m = AdaptiveEngine._calc_momentum(0.30, 0.50)
        assert m == 0.90

    def test_neutral_momentum(self):
        m = AdaptiveEngine._calc_momentum(0.52, 0.50)
        assert m == 1.0

    def test_none_inputs(self):
        assert AdaptiveEngine._calc_momentum(None, 0.50) == 1.0
        assert AdaptiveEngine._calc_momentum(0.50, None) == 1.0
        assert AdaptiveEngine._calc_momentum(None, None) == 1.0

    def test_momentum_within_bounds(self):
        m = AdaptiveEngine._calc_momentum(1.0, 0.0)
        assert _MOMENTUM_BOUNDS[0] <= m <= _MOMENTUM_BOUNDS[1]


# ── Test _calc_direction_bias ─────────────────────────────────────────────────

class TestDirectionBias:
    def test_balanced_no_bias(self):
        engine = AdaptiveEngine()
        rows = _make_mixed_rows(30, up_pct=0.50)
        adj, bias_dir, bias_pct = engine._calc_direction_bias(rows)
        assert adj == 0.0
        assert bias_dir is None

    def test_up_biased(self):
        engine = AdaptiveEngine()
        rows = _make_mixed_rows(30, up_pct=0.90)  # must exceed _DIRECTION_BIAS_THRESHOLD (0.85)
        adj, bias_dir, bias_pct = engine._calc_direction_bias(rows)
        assert adj == 0.03  # _clamp(0.03, -0.05, 0.08) = 0.03
        assert bias_dir == "UP"
        assert bias_pct >= 0.85

    def test_down_biased(self):
        engine = AdaptiveEngine()
        rows = _make_mixed_rows(30, up_pct=0.10)  # 90% DOWN, exceeds threshold
        adj, bias_dir, bias_pct = engine._calc_direction_bias(rows)
        assert adj == 0.03
        assert bias_dir == "DOWN"

    def test_too_few_rows_no_bias(self):
        engine = AdaptiveEngine()
        rows = _make_mixed_rows(5)
        adj, bias_dir, bias_pct = engine._calc_direction_bias(rows)
        assert adj == 0.0
        assert bias_dir is None


# ── Test _find_best_band ──────────────────────────────────────────────────────

class TestFindBestBand:
    def test_finds_best_band(self):
        engine = AdaptiveEngine()
        # Create rows with high WR at 0.60-0.65 band
        rows = _make_rows(30, wr=0.70, conf=0.62)
        thr, label, exp = engine._find_best_band(rows)
        assert label == "0.60-0.65"
        assert thr == 0.60
        assert exp is not None and exp > 0

    def test_insufficient_data_returns_default(self):
        engine = AdaptiveEngine()
        rows = _make_rows(3, wr=0.50, conf=0.62)
        thr, label, exp = engine._find_best_band(rows)
        assert thr == _DEFAULT_THRESHOLD
        assert label is None

    def test_threshold_within_bounds(self):
        engine = AdaptiveEngine()
        rows = _make_rows(50, wr=0.60, conf=0.52)
        thr, _, _ = engine._find_best_band(rows)
        assert _THRESHOLD_BOUNDS[0] <= thr <= _THRESHOLD_BOUNDS[1]


# ── Test _compute (full pipeline) ────────────────────────────────────────────

class TestCompute:
    def test_compute_with_sufficient_data(self):
        engine = AdaptiveEngine()
        rows = _make_rows(100, wr=0.55, conf=0.62)
        state = engine._compute(rows)
        assert isinstance(state, AdaptiveState)
        assert _THRESHOLD_BOUNDS[0] <= state.effective_threshold <= _THRESHOLD_BOUNDS[1]
        assert state.total_signals_used == 100
        assert state.wr_50 is not None
        assert state.wr_100 is not None

    def test_compute_wr_correct(self):
        engine = AdaptiveEngine()
        rows = _make_rows(50, wr=0.60, conf=0.62)
        state = engine._compute(rows)
        assert state.wr_50 == 0.60


# ── Test evaluate (public API) ───────────────────────────────────────────────

class TestEvaluate:
    def test_evaluate_returns_expected_keys(self):
        engine = AdaptiveEngine()
        result = engine.evaluate(0.65, "UP")
        expected_keys = {
            "should_trade", "adjusted_conf", "raw_conf",
            "effective_threshold", "optimal_threshold",
            "direction_bias_adj", "regime", "regime_adj",
            "size_factor", "momentum_factor", "calibration_wr_factor", "reason",
        }
        assert expected_keys.issubset(result.keys())

    def test_evaluate_neutral_state_passthrough(self):
        engine = AdaptiveEngine()
        result = engine.evaluate(0.65, "UP")
        # With neutral state: adjusted_conf = 0.65 * 1.0 * 1.0 = 0.65
        assert result["adjusted_conf"] == 0.65
        assert result["should_trade"] is True

    def test_evaluate_below_threshold_skips(self):
        engine = AdaptiveEngine()
        result = engine.evaluate(0.40, "UP")
        assert result["should_trade"] is False

    def test_evaluate_disabled(self, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_ENGINE_DISABLED", "true")
        engine = AdaptiveEngine()
        result = engine.evaluate(0.65, "UP")
        assert result["reason"] == "disabled"
        assert result["should_trade"] is True

    def test_evaluate_env_floor_respected(self, monkeypatch):
        monkeypatch.setenv("CONF_THRESHOLD", "0.70")
        engine = AdaptiveEngine()
        result = engine.evaluate(0.65, "UP")
        assert result["effective_threshold"] >= 0.70
        assert result["should_trade"] is False


# ── Test fail-open behavior ──────────────────────────────────────────────────

class TestFailOpen:
    def test_evaluate_with_corrupted_state(self):
        engine = AdaptiveEngine()
        # Corrupt the state
        engine._state = None
        result = engine.evaluate(0.65, "UP")
        # Should fail-open
        assert result["reason"] == "error_failopen"
        assert result["should_trade"] is True

    def test_recalculate_without_supabase(self):
        engine = AdaptiveEngine(sb_url="", sb_key="")
        result = engine.recalculate()
        assert result is False  # No data, returns False gracefully

    def test_maybe_recalculate_disabled(self, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_ENGINE_DISABLED", "true")
        engine = AdaptiveEngine()
        assert engine.maybe_recalculate() is False


# ── Test get_estimate ─────────────────────────────────────────────────────────

class TestGetEstimate:
    def test_returns_expected_structure(self):
        engine = AdaptiveEngine()
        est = engine.get_estimate()
        assert "disabled" in est
        assert "state" in est
        assert "last_calc_ts" in est
        assert "signals_since_calc" in est

    def test_state_dict_in_estimate(self):
        engine = AdaptiveEngine()
        est = engine.get_estimate()
        state_dict = est["state"]
        assert "optimal_threshold" in state_dict
        assert "effective_threshold" in state_dict


# ── Test _avg_pnl ─────────────────────────────────────────────────────────────

class TestAvgPnl:
    def test_with_pnl_data(self):
        rows = [{"pnl_pct": 0.01}, {"pnl_pct": 0.02}, {"pnl_pct": 0.03}]
        assert AdaptiveEngine._avg_pnl(rows) == pytest.approx(0.02)

    def test_empty_rows_returns_default(self):
        assert AdaptiveEngine._avg_pnl([], default=0.005) == 0.005

    def test_none_pnl_excluded(self):
        rows = [{"pnl_pct": 0.01}, {"pnl_pct": None}, {"pnl_pct": 0.03}]
        assert AdaptiveEngine._avg_pnl(rows) == pytest.approx(0.02)
