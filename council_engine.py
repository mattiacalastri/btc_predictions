"""
Council Engine — Fase 2: Multi-member AI deliberation layer
Members: TECNICO (Claude Sonnet), SENTIMENT (Gemini Flash), QUANT (XGBoost local)

Designed to be imported by app.py. Zero circular imports.
"""
import os
import re
import json
import threading
import datetime as _dt
from concurrent.futures import ThreadPoolExecutor, as_completed

import certifi
import requests

# ── Constants ─────────────────────────────────────────────────────────────────

COUNCIL_MEMBERS = {
    "TECNICO":   {"model": "claude-sonnet-4-6",   "weight": 0.30},
    "SENTIMENT": {"model": "gemini-2.0-flash",        "weight": 0.15},
    "QUANT":     {"model": "xgboost-local",        "weight": 0.25},
}

_TECNICO_SYSTEM = (
    "You are the Technical Analyst on the BTC Predictor Council.\n"
    "Your only job: read price structure and predict the next 30-minute BTC direction.\n\n"
    "You see: RSI, EMA(9/21/50), MACD, Bollinger Bands, 5m/15m/4h candlestick patterns,\n"
    "multi-timeframe consensus, order book imbalance.\n\n"
    "You do NOT see news, sentiment, or on-chain data. They are not your domain.\n"
    "Trade what you see, not what you feel.\n\n"
    "Rules:\n"
    "- If multi-timeframe signals conflict, lower your confidence. Never force a call.\n"
    "- OB imbalance > 60% on 5 levels = directional signal, not confirmation.\n"
    "- 3 consecutive candles same direction + declining volume = reversal candidate.\n\n"
    'Output (JSON only, no extra text):\n'
    '{"direction": "UP|DOWN", "confidence": 0.XX, "reasoning": "max 100 chars"}\n\n'
    "Your vote is logged on-chain. Be precise."
)

_SENTIMENT_SYSTEM = (
    "You are the Sentiment Reader on the BTC Predictor Council.\n"
    "Your only job: decode crowd psychology and predict the next 30-minute BTC direction.\n\n"
    "You see: Fear & Greed index, Binance funding rate, long/short ratio,\n"
    "macro news headlines, social media sentiment score.\n\n"
    "You do NOT see price charts or on-chain data. You read the crowd, not the tape.\n\n"
    "Rules:\n"
    "- Fear&Greed extremes (<25 or >75) are contrarian signals, not trend confirmations.\n"
    "- Funding rate > 0.08% = overleveraged longs = reversal risk even in uptrend.\n"
    "- L/S ratio > 60% long = crowd overcrowded = short squeeze or cascade both possible.\n"
    "- When the crowd is certain, question it.\n\n"
    'Output (JSON only, no extra text):\n'
    '{"direction": "UP|DOWN", "confidence": 0.XX, "reasoning": "max 100 chars"}\n\n'
    "Your vote is logged on-chain. Be precise."
)


# ── Prompt builders ────────────────────────────────────────────────────────────

def _build_tecnico_message(payload: dict) -> str:
    """Format technical market data for TECNICO."""
    lines = ["Current BTC market data (technical signals only):"]
    for k in [
        "rsi14", "ema9", "ema21", "ema50", "macd", "macd_signal",
        "bb_upper", "bb_lower", "bb_mid",
        "technical_score", "technical_bias",
        "ob_imbalance", "ob_bid_pct",
        "candle_pattern_5m", "candle_pattern_15m", "candle_pattern_4h",
        "mtf_consensus", "atr_pct", "signal_price",
    ]:
        v = payload.get(k)
        if v is not None:
            lines.append(f"  {k}: {v}")
    lines.append("\nBased on this data, what is the most likely 30-minute BTC direction?")
    return "\n".join(lines)


def _build_sentiment_message(payload: dict) -> str:
    """Format sentiment/macro market data for SENTIMENT."""
    lines = ["Current BTC market data (sentiment and macro signals only):"]
    for k in [
        "fear_greed", "fear_greed_value",
        "funding_rate", "ls_ratio", "long_short_ratio",
        "news_sentiment", "social_sentiment",
        "open_interest", "oi_change",
    ]:
        v = payload.get(k)
        if v is not None:
            lines.append(f"  {k}: {v}")
    lines.append("\nBased on this data, what is the most likely 30-minute BTC direction?")
    return "\n".join(lines)


# ── JSON parser ────────────────────────────────────────────────────────────────

def _parse_llm_json(text: str) -> dict:
    """Extract JSON dict from LLM response text (handles markdown code blocks)."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r'\{[^{}]+\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


# ── Member callers ─────────────────────────────────────────────────────────────

def call_tecnico(payload: dict) -> dict:
    """Call Claude Sonnet for technical analysis. Returns vote dict."""
    member = "TECNICO"
    model = COUNCIL_MEMBERS[member]["model"]
    weight = COUNCIL_MEMBERS[member]["weight"]
    try:
        import anthropic
        import httpx
        client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            http_client=httpx.Client(verify=certifi.where()),
        )
        msg = client.messages.create(
            model=model,
            max_tokens=256,
            system=_TECNICO_SYSTEM,
            messages=[{"role": "user", "content": _build_tecnico_message(payload)}],
            timeout=30.0,
        )
        raw_text = msg.content[0].text if msg.content else ""
        parsed = _parse_llm_json(raw_text)
        direction = str(parsed.get("direction", "")).upper()
        if direction not in ("UP", "DOWN"):
            direction = "ABSTAIN"
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.5))))
        reasoning = str(parsed.get("reasoning", ""))[:500]
        return {
            "member": member,
            "model_used": model,
            "direction": direction,
            "confidence": confidence,
            "weight": weight,
            "reasoning": reasoning,
            "raw_response": {"text": raw_text[:1000]},
            "error": None,
        }
    except Exception as e:
        return {
            "member": member,
            "model_used": model,
            "direction": "ABSTAIN",
            "confidence": 0.5,
            "weight": weight,
            "reasoning": f"error: {str(e)[:100]}",
            "raw_response": {"error": str(e)},
            "error": str(e),
        }


def call_sentiment(payload: dict) -> dict:
    """Call Gemini Flash for sentiment analysis. Returns vote dict."""
    member = "SENTIMENT"
    model = COUNCIL_MEMBERS[member]["model"]
    weight = COUNCIL_MEMBERS[member]["weight"]
    try:
        import requests as _requests
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        _gemini_model = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
        _api_ver = os.environ.get("GEMINI_API_VERSION", "v1")
        _url = f"https://generativelanguage.googleapis.com/{_api_ver}/models/{_gemini_model}:generateContent?key={gemini_key}"
        _body = {
            "system_instruction": {"parts": [{"text": _SENTIMENT_SYSTEM}]},
            "contents": [{"parts": [{"text": _build_sentiment_message(payload)}]}],
            "generationConfig": {"maxOutputTokens": 256, "temperature": 0.3},
        }
        _resp = _requests.post(_url, json=_body, timeout=30, verify=certifi.where())
        _resp.raise_for_status()
        raw_text = _resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        parsed = _parse_llm_json(raw_text)
        direction = str(parsed.get("direction", "")).upper()
        if direction not in ("UP", "DOWN"):
            direction = "ABSTAIN"
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.5))))
        reasoning = str(parsed.get("reasoning", ""))[:500]
        return {
            "member": member,
            "model_used": model,
            "direction": direction,
            "confidence": confidence,
            "weight": weight,
            "reasoning": reasoning,
            "raw_response": {"text": raw_text[:1000]},
            "error": None,
        }
    except Exception as e:
        return {
            "member": member,
            "model_used": model,
            "direction": "ABSTAIN",
            "confidence": 0.5,
            "weight": weight,
            "reasoning": f"error: {str(e)[:100]}",
            "raw_response": {"error": str(e)},
            "error": str(e),
        }


def call_quant(payload: dict) -> dict:
    """Use pre-computed XGBoost probability from payload. Returns vote dict.

    app.py pre-computes xgb_prob_up via _run_xgb_gate() and passes it in
    payload['xgb_prob_up'] before calling run_round1(). Default: 0.5 (neutral).
    """
    member = "QUANT"
    model = COUNCIL_MEMBERS[member]["model"]
    weight = COUNCIL_MEMBERS[member]["weight"]
    try:
        xgb_prob_up = max(0.0, min(1.0, float(payload.get("xgb_prob_up", 0.5))))
        if xgb_prob_up > 0.5:
            direction = "UP"
            confidence = xgb_prob_up
        elif xgb_prob_up < 0.5:
            direction = "DOWN"
            confidence = 1.0 - xgb_prob_up
        else:
            direction = "ABSTAIN"
            confidence = 0.5
        reasoning = f"XGB P(UP)={xgb_prob_up:.3f}"
        return {
            "member": member,
            "model_used": model,
            "direction": direction,
            "confidence": confidence,
            "weight": weight,
            "reasoning": reasoning,
            "raw_response": {"xgb_prob_up": xgb_prob_up},
            "error": None,
        }
    except Exception as e:
        return {
            "member": member,
            "model_used": model,
            "direction": "ABSTAIN",
            "confidence": 0.5,
            "weight": weight,
            "reasoning": f"error: {str(e)[:100]}",
            "raw_response": {"error": str(e)},
            "error": str(e),
        }


# ── Round execution ────────────────────────────────────────────────────────────

def run_round1(payload: dict, timeout: float = 30.0) -> list:
    """Run all 3 council members in parallel. Returns list of vote dicts."""
    tasks = {
        "TECNICO": call_tecnico,
        "SENTIMENT": call_sentiment,
        "QUANT": call_quant,
    }
    votes = []
    futures_map = {}
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="council") as executor:
        for member, fn in tasks.items():
            futures_map[executor.submit(fn, payload)] = member
        for future in as_completed(futures_map, timeout=timeout + 5):
            member = futures_map[future]
            try:
                vote = future.result(timeout=timeout)
                votes.append(vote)
            except Exception as e:
                votes.append({
                    "member": member,
                    "model_used": COUNCIL_MEMBERS[member]["model"],
                    "direction": "ABSTAIN",
                    "confidence": 0.5,
                    "weight": COUNCIL_MEMBERS[member]["weight"],
                    "reasoning": f"timeout/error: {str(e)[:100]}",
                    "raw_response": {"error": str(e)},
                    "error": str(e),
                })
    return votes


# ── Vote aggregation ───────────────────────────────────────────────────────────

def compute_weighted_vote(votes: list) -> dict:
    """Compute weighted vote from member votes.

    UP=+1, DOWN=-1, ABSTAIN=excluded from weight sum.
    score = Σ(weight_i × numeric_i) / Σ(weight_i of non-abstain)
    score > 0.15  → UP
    score < -0.15 → DOWN
    else          → SKIP (low conviction)

    council_confidence = 0.50 + (|score| × 0.30), clamped [0.50, 0.80]
    agreement_score    = fraction of non-abstain votes matching majority
    """
    _NUMERIC = {"UP": 1.0, "DOWN": -1.0}
    weighted_sum = 0.0
    weight_sum = 0.0
    direction_counts: dict = {"UP": 0, "DOWN": 0, "ABSTAIN": 0}

    for vote in votes:
        direction = vote.get("direction", "ABSTAIN")
        weight = float(vote.get("weight", 0.0))
        direction_counts[direction] = direction_counts.get(direction, 0) + 1
        if direction in _NUMERIC:
            weighted_sum += weight * _NUMERIC[direction]
            weight_sum += weight

    if weight_sum == 0:
        return {
            "direction": "SKIP",
            "council_confidence": 0.50,
            "agreement_score": 0.0,
            "score": 0.0,
            "votes_summary": direction_counts,
        }

    score = weighted_sum / weight_sum

    if score > 0.15:
        final_direction = "UP"
    elif score < -0.15:
        final_direction = "DOWN"
    else:
        final_direction = "SKIP"

    council_confidence = min(0.80, max(0.50, 0.50 + abs(score) * 0.30))

    non_abstain = [v for v in votes if v.get("direction") != "ABSTAIN"]
    if non_abstain and final_direction != "SKIP":
        matching = sum(1 for v in non_abstain if v.get("direction") == final_direction)
        agreement_score = matching / len(non_abstain)
    else:
        agreement_score = 0.0

    return {
        "direction": final_direction,
        "council_confidence": round(council_confidence, 4),
        "agreement_score": round(agreement_score, 4),
        "score": round(score, 4),
        "votes_summary": direction_counts,
    }


# ── Supabase logging ───────────────────────────────────────────────────────────

def log_votes_async(votes: list, signal_hash: str, prediction_id=None):
    """Insert council votes into council_votes table. Non-blocking fire-and-forget."""
    def _do_log():
        try:
            sb_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
            sb_key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY", "")
            if not sb_url or not sb_key:
                return
            rows = []
            for vote in votes:
                rows.append({
                    "prediction_id": prediction_id,
                    "signal_hash": signal_hash,
                    "round": 1,
                    "member": vote.get("member"),
                    "model_used": vote.get("model_used"),
                    "direction": vote.get("direction"),
                    "confidence": vote.get("confidence"),
                    "weight": vote.get("weight"),
                    "reasoning": (vote.get("reasoning") or "")[:500],
                    "raw_response": vote.get("raw_response"),
                })
            requests.post(
                f"{sb_url}/rest/v1/council_votes",
                json=rows,
                headers={
                    "apikey": sb_key,
                    "Authorization": f"Bearer {sb_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                timeout=8,
                verify=certifi.where(),
            )
        except Exception:
            pass  # fire-and-forget: never raise

    threading.Thread(target=_do_log, daemon=True).start()
