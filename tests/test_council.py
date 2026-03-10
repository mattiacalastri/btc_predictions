"""
Unit tests for Council Engine (council_engine.py).

Tests JSON parsing, prompt builders, QUANT member logic,
weighted vote aggregation, and edge cases. No API calls required —
TECNICO and SENTIMENT are tested only for error handling paths.
"""

import os
import sys
import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from council_engine import (
    _parse_llm_json,
    _build_tecnico_message,
    _build_sentiment_message,
    call_quant,
    compute_weighted_vote,
    COUNCIL_MEMBERS,
)


# ── Test _parse_llm_json ────────────────────────────────────────────────────

class TestParseLlmJson:
    def test_plain_json(self):
        result = _parse_llm_json('{"direction": "UP", "confidence": 0.75}')
        assert result["direction"] == "UP"
        assert result["confidence"] == 0.75

    def test_markdown_code_block(self):
        text = '```json\n{"direction": "DOWN", "confidence": 0.60}\n```'
        result = _parse_llm_json(text)
        assert result["direction"] == "DOWN"

    def test_text_around_json(self):
        text = 'Based on my analysis: {"direction": "UP", "confidence": 0.80, "reasoning": "strong trend"} end.'
        result = _parse_llm_json(text)
        assert result["direction"] == "UP"
        assert result["confidence"] == 0.80

    def test_empty_string_returns_empty(self):
        assert _parse_llm_json("") == {}

    def test_none_input_returns_empty(self):
        assert _parse_llm_json(None) == {}

    def test_invalid_json_returns_empty(self):
        assert _parse_llm_json("this is not json at all") == {}

    def test_nested_json_ignored(self):
        # _parse_llm_json uses [^{}]+ so nested braces are not matched
        text = '{"direction": "UP", "confidence": 0.70, "reasoning": "test"}'
        result = _parse_llm_json(text)
        assert result["direction"] == "UP"


# ── Test _build_tecnico_message ──────────────────────────────────────────────

class TestBuildTecnicoMessage:
    def test_includes_technical_keys(self):
        payload = {
            "rsi14": 55.0,
            "ema9": 80000,
            "ema21": 79500,
            "macd": 50.0,
            "signal_price": 80100,
        }
        msg = _build_tecnico_message(payload)
        assert "rsi14: 55.0" in msg
        assert "ema9: 80000" in msg
        assert "signal_price: 80100" in msg
        assert "30-minute BTC direction" in msg

    def test_excludes_none_values(self):
        payload = {"rsi14": 55.0, "ema9": None}
        msg = _build_tecnico_message(payload)
        assert "rsi14: 55.0" in msg
        assert "ema9" not in msg

    def test_excludes_sentiment_keys(self):
        payload = {"rsi14": 55.0, "fear_greed": 45, "funding_rate": 0.01}
        msg = _build_tecnico_message(payload)
        assert "rsi14" in msg
        assert "fear_greed" not in msg
        assert "funding_rate" not in msg

    def test_empty_payload(self):
        msg = _build_tecnico_message({})
        assert "Current BTC market data" in msg
        assert "30-minute BTC direction" in msg


# ── Test _build_sentiment_message ────────────────────────────────────────────

class TestBuildSentimentMessage:
    def test_includes_sentiment_keys(self):
        payload = {
            "fear_greed": 35,
            "funding_rate": 0.05,
            "ls_ratio": 65,
            "news_sentiment": "negative",
        }
        msg = _build_sentiment_message(payload)
        assert "fear_greed: 35" in msg
        assert "funding_rate: 0.05" in msg
        assert "ls_ratio: 65" in msg

    def test_excludes_technical_keys(self):
        payload = {"fear_greed": 35, "rsi14": 55, "ema9": 80000}
        msg = _build_sentiment_message(payload)
        assert "fear_greed" in msg
        assert "rsi14" not in msg
        assert "ema9" not in msg


# ── Test call_quant ──────────────────────────────────────────────────────────

class TestCallQuant:
    def test_up_signal(self):
        vote = call_quant({"xgb_prob_up": 0.70})
        assert vote["member"] == "QUANT"
        assert vote["direction"] == "UP"
        assert vote["confidence"] == 0.70
        assert vote["error"] is None

    def test_down_signal(self):
        vote = call_quant({"xgb_prob_up": 0.30})
        assert vote["direction"] == "DOWN"
        assert vote["confidence"] == 0.70  # 1.0 - 0.30

    def test_neutral_signal(self):
        vote = call_quant({"xgb_prob_up": 0.50})
        assert vote["direction"] == "ABSTAIN"
        assert vote["confidence"] == 0.50

    def test_missing_xgb_prob_defaults_neutral(self):
        vote = call_quant({})
        assert vote["direction"] == "ABSTAIN"
        assert vote["confidence"] == 0.50

    def test_clamped_to_range(self):
        vote = call_quant({"xgb_prob_up": 1.5})
        assert vote["confidence"] <= 1.0
        vote2 = call_quant({"xgb_prob_up": -0.5})
        assert vote2["confidence"] >= 0.0

    def test_weight_matches_config(self):
        vote = call_quant({"xgb_prob_up": 0.70})
        assert vote["weight"] == COUNCIL_MEMBERS["QUANT"]["weight"]

    def test_reasoning_contains_prob(self):
        vote = call_quant({"xgb_prob_up": 0.65})
        assert "0.650" in vote["reasoning"]

    def test_error_handling(self):
        # xgb_prob_up as non-numeric should trigger exception
        vote = call_quant({"xgb_prob_up": "not_a_number"})
        assert vote["direction"] == "ABSTAIN"
        assert vote["error"] is not None


# ── Test call_tecnico error path (no API key) ───────────────────────────────

class TestCallTecnicoErrorPath:
    def test_no_api_key_returns_abstain(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from council_engine import call_tecnico
        vote = call_tecnico({"rsi14": 55})
        # Without a valid API key, it should error and return ABSTAIN
        assert vote["member"] == "TECNICO"
        assert vote["direction"] == "ABSTAIN"
        assert vote["error"] is not None


# ── Test call_sentiment error path (no API key) ─────────────────────────────

class TestCallSentimentErrorPath:
    def test_no_api_key_returns_abstain(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        from council_engine import call_sentiment
        vote = call_sentiment({"fear_greed": 35})
        assert vote["member"] == "SENTIMENT"
        assert vote["direction"] == "ABSTAIN"
        assert vote["error"] is not None or "missing_api_key" in vote["reasoning"]


# ── Test compute_weighted_vote ───────────────────────────────────────────────

class TestComputeWeightedVote:
    def test_unanimous_up(self):
        votes = [
            {"direction": "UP", "weight": 0.30, "confidence": 0.80},
            {"direction": "UP", "weight": 0.15, "confidence": 0.70},
            {"direction": "UP", "weight": 0.25, "confidence": 0.75},
        ]
        result = compute_weighted_vote(votes)
        assert result["direction"] == "UP"
        assert result["score"] == pytest.approx(1.0, abs=0.01)
        assert result["agreement_score"] == 1.0
        assert result["council_confidence"] > 0.50

    def test_unanimous_down(self):
        votes = [
            {"direction": "DOWN", "weight": 0.30, "confidence": 0.80},
            {"direction": "DOWN", "weight": 0.15, "confidence": 0.70},
            {"direction": "DOWN", "weight": 0.25, "confidence": 0.75},
        ]
        result = compute_weighted_vote(votes)
        assert result["direction"] == "DOWN"
        assert result["score"] == pytest.approx(-1.0, abs=0.01)

    def test_split_vote_becomes_skip(self):
        votes = [
            {"direction": "UP", "weight": 0.30, "confidence": 0.70},
            {"direction": "DOWN", "weight": 0.25, "confidence": 0.70},
            {"direction": "ABSTAIN", "weight": 0.15, "confidence": 0.50},
        ]
        result = compute_weighted_vote(votes)
        # score = (0.30*1 + 0.25*(-1)) / (0.30+0.25) = 0.05/0.55 ~ 0.09
        # 0.09 < 0.15 → SKIP
        assert result["direction"] == "SKIP"

    def test_all_abstain_is_skip(self):
        votes = [
            {"direction": "ABSTAIN", "weight": 0.30, "confidence": 0.50},
            {"direction": "ABSTAIN", "weight": 0.15, "confidence": 0.50},
            {"direction": "ABSTAIN", "weight": 0.25, "confidence": 0.50},
        ]
        result = compute_weighted_vote(votes)
        assert result["direction"] == "SKIP"
        assert result["score"] == 0.0
        assert result["agreement_score"] == 0.0

    def test_empty_votes_is_skip(self):
        result = compute_weighted_vote([])
        assert result["direction"] == "SKIP"
        assert result["score"] == 0.0

    def test_single_up_vote(self):
        votes = [{"direction": "UP", "weight": 0.30, "confidence": 0.80}]
        result = compute_weighted_vote(votes)
        assert result["direction"] == "UP"
        assert result["score"] == pytest.approx(1.0)
        assert result["agreement_score"] == 1.0

    def test_council_confidence_bounds(self):
        # Max score = 1.0 → confidence = 0.50 + 1.0 * 0.30 = 0.80
        votes = [{"direction": "UP", "weight": 1.0, "confidence": 1.0}]
        result = compute_weighted_vote(votes)
        assert result["council_confidence"] <= 0.80
        assert result["council_confidence"] >= 0.50

    def test_weighted_majority(self):
        # TECNICO (0.30) UP + QUANT (0.25) UP vs SENTIMENT (0.15) DOWN
        votes = [
            {"direction": "UP", "weight": 0.30, "confidence": 0.70},
            {"direction": "UP", "weight": 0.25, "confidence": 0.65},
            {"direction": "DOWN", "weight": 0.15, "confidence": 0.60},
        ]
        result = compute_weighted_vote(votes)
        # score = (0.30+0.25-0.15) / (0.30+0.25+0.15) = 0.40/0.70 ~ 0.571
        assert result["direction"] == "UP"
        assert result["score"] > 0.15

    def test_agreement_score_partial(self):
        votes = [
            {"direction": "UP", "weight": 0.30, "confidence": 0.70},
            {"direction": "UP", "weight": 0.25, "confidence": 0.65},
            {"direction": "DOWN", "weight": 0.15, "confidence": 0.60},
        ]
        result = compute_weighted_vote(votes)
        # 2 out of 3 non-abstain match UP
        assert result["agreement_score"] == pytest.approx(2 / 3, abs=0.01)

    def test_votes_summary_counts(self):
        votes = [
            {"direction": "UP", "weight": 0.30},
            {"direction": "DOWN", "weight": 0.15},
            {"direction": "ABSTAIN", "weight": 0.25},
        ]
        result = compute_weighted_vote(votes)
        assert result["votes_summary"]["UP"] == 1
        assert result["votes_summary"]["DOWN"] == 1
        assert result["votes_summary"]["ABSTAIN"] == 1

    def test_missing_direction_treated_as_abstain(self):
        votes = [
            {"weight": 0.30},  # no direction key
            {"direction": "UP", "weight": 0.25},
        ]
        result = compute_weighted_vote(votes)
        # First vote: direction="ABSTAIN" (default), not in _NUMERIC → excluded
        assert result["direction"] == "UP"
