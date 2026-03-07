"""
adaptive_engine.py — Adaptive Calibration Engine (ACE)

Sits between the LLM signal and trade execution, auto-calibrating
confidence thresholds, direction bias, market regime, and momentum.

Recalculates every 50 new signals OR every hour (whichever comes first).
Thread-safe, fail-open: any error → static defaults (0.56, no adjustments).

Kill switch: env ADAPTIVE_ENGINE_DISABLED=true → bypass entirely.
"""

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
import ssl

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

logger = logging.getLogger("adaptive_engine")

# ── Defaults & bounds ─────────────────────────────────────────────────────────
_DEFAULT_THRESHOLD = 0.62
_THRESHOLD_BOUNDS = (0.50, 0.70)
_MOMENTUM_BOUNDS = (0.95, 1.05)
_DIRECTION_ADJ_BOUNDS = (-0.05, 0.08)
_REGIME_ADJ_BOUNDS = (-0.05, 0.08)
_MIN_SIGNALS_FOR_CALC = 30
_MIN_SIGNALS_PER_BAND = 5
_RECALC_INTERVAL_SEC = 3600       # 1 hour
_RECALC_MIN_NEW_SIGNALS = 50
_DIRECTION_WINDOW = 30            # last N signals for bias detection
_DIRECTION_BIAS_THRESHOLD = 0.85  # 85% = biased (relaxed: trending markets legitimately cluster)

# Confidence bands: (lower, upper)
_CONF_BANDS = [
    (0.50, 0.55),
    (0.55, 0.60),
    (0.60, 0.65),
    (0.65, 0.70),
    (0.70, 1.00),
]


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


class AdaptiveState:
    """Immutable snapshot of adaptive parameters. Thread-safe to read."""
    __slots__ = (
        "optimal_threshold", "effective_threshold", "direction_bias_adj",
        "regime", "regime_adj", "regime_size_factor", "momentum_factor",
        "calibration_wr_factor", "wr_50", "wr_100", "wr_200",
        "wr_up", "wr_down", "wr_recent_10", "wr_baseline_50",
        "bias_direction", "bias_pct", "best_band_label",
        "best_band_expectancy", "total_signals_used", "updated_at",
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    def to_dict(self):
        return {s: getattr(self, s) for s in self.__slots__}

    @staticmethod
    def neutral():
        """Return a neutral (no-adjustment) state."""
        return AdaptiveState(
            optimal_threshold=_DEFAULT_THRESHOLD,
            effective_threshold=_DEFAULT_THRESHOLD,
            direction_bias_adj=0.0,
            regime="UNKNOWN", regime_adj=0.0, regime_size_factor=1.0,
            momentum_factor=1.0, calibration_wr_factor=1.0,
            wr_50=None, wr_100=None, wr_200=None,
            wr_up=None, wr_down=None,
            wr_recent_10=None, wr_baseline_50=None,
            bias_direction=None, bias_pct=None,
            best_band_label=None, best_band_expectancy=None,
            total_signals_used=0, updated_at=None,
        )


class AdaptiveEngine:
    """
    Adaptive Calibration Engine.

    Usage:
        engine = AdaptiveEngine(sb_url, sb_key)
        result = engine.evaluate(raw_confidence, direction)
        # result = {"should_trade": bool, "adjusted_conf": float,
        #           "effective_threshold": float, "size_factor": float, ...}
    """

    def __init__(self, sb_url: str = "", sb_key: str = "", table: str = "btc_predictions"):
        self._sb_url = sb_url.rstrip("/")
        self._sb_key = sb_key
        self._table = table
        self._lock = threading.Lock()
        self._state: AdaptiveState = AdaptiveState.neutral()
        self._last_calc_ts: float = 0.0
        self._signals_since_calc: int = 0

    @property
    def disabled(self) -> bool:
        return os.environ.get("ADAPTIVE_ENGINE_DISABLED", "").lower() == "true"

    @property
    def state(self) -> AdaptiveState:
        return self._state

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate(self, raw_confidence: float, direction: str) -> dict:
        """
        Evaluate whether a trade should proceed, given adaptive calibration.
        Fail-open: on any error, returns should_trade=True with no adjustments.
        """
        if self.disabled:
            return self._passthrough(raw_confidence, direction, reason="disabled")
        try:
            st = self._state
            adj_conf = raw_confidence * st.calibration_wr_factor * st.momentum_factor
            env_floor = float(os.environ.get("CONF_THRESHOLD", str(_DEFAULT_THRESHOLD)))
            eff_threshold = st.optimal_threshold + st.regime_adj + st.direction_bias_adj

            # Apply per-direction bias adjustment
            if st.bias_direction and direction == st.bias_direction:
                pass  # already included in direction_bias_adj (positive = harder)
            elif st.bias_direction and direction != st.bias_direction:
                # Underrepresented direction: subtract double the adj to make it easier
                eff_threshold = eff_threshold - 2 * abs(st.direction_bias_adj)

            eff_threshold = _clamp(eff_threshold, *_THRESHOLD_BOUNDS)
            # Env floor: if env CONF_THRESHOLD > adaptive, env wins
            eff_threshold = max(eff_threshold, env_floor)

            # Starvation protection: if WR/momentum penalties reduce adj_conf below
            # env_floor but raw_confidence passes env_floor, use raw_confidence.
            # Prevents deadlock where low WR makes ALL signals untradeable.
            effective_conf = adj_conf
            if adj_conf < env_floor and raw_confidence >= env_floor:
                effective_conf = raw_confidence  # bypass WR/momentum penalty at floor

            should_trade = effective_conf >= eff_threshold
            return {
                "should_trade": should_trade,
                "adjusted_conf": round(adj_conf, 4),
                "effective_conf": round(effective_conf, 4),
                "raw_conf": raw_confidence,
                "effective_threshold": round(eff_threshold, 4),
                "optimal_threshold": round(st.optimal_threshold, 4),
                "direction_bias_adj": round(st.direction_bias_adj, 4),
                "regime": st.regime,
                "regime_adj": round(st.regime_adj, 4),
                "size_factor": round(st.regime_size_factor, 4),
                "momentum_factor": round(st.momentum_factor, 4),
                "calibration_wr_factor": round(st.calibration_wr_factor, 4),
                "reason": "adaptive",
            }
        except Exception:
            logger.exception("[ACE] evaluate error — fail-open")
            return self._passthrough(raw_confidence, direction, reason="error_failopen")

    def maybe_recalculate(self, trigger: str = "ghost_batch") -> bool:
        """Recalculate if enough new signals or enough time has passed."""
        if self.disabled:
            return False
        with self._lock:
            self._signals_since_calc += 1
            count = self._signals_since_calc
            last_ts = self._last_calc_ts
        now = time.time()
        enough_signals = count >= _RECALC_MIN_NEW_SIGNALS
        enough_time = (now - last_ts) >= _RECALC_INTERVAL_SEC
        if enough_signals or enough_time:
            return self.recalculate(trigger=trigger)
        return False

    def recalculate(self, trigger: str = "manual") -> bool:
        """Force a full recalculation from Supabase data."""
        if self.disabled:
            return False
        try:
            rows = self._fetch_signals()
            if len(rows) < _MIN_SIGNALS_FOR_CALC:
                logger.info(f"[ACE] Not enough signals ({len(rows)}/{_MIN_SIGNALS_FOR_CALC})")
                return False

            new_state = self._compute(rows)
            with self._lock:
                self._state = new_state
                self._last_calc_ts = time.time()
                self._signals_since_calc = 0

            self._persist_state(new_state, trigger, len(rows))
            logger.info(
                f"[ACE] Recalculated ({trigger}): threshold={new_state.effective_threshold:.3f} "
                f"regime={new_state.regime} momentum={new_state.momentum_factor:.3f} "
                f"signals={len(rows)}"
            )
            return True
        except Exception:
            logger.exception("[ACE] recalculate error — keeping current state")
            return False

    def get_estimate(self) -> dict:
        """Return current state as dict (for /adaptive-estimate endpoint)."""
        st = self._state
        return {
            "disabled": self.disabled,
            "state": st.to_dict(),
            "last_calc_ts": self._last_calc_ts,
            "signals_since_calc": self._signals_since_calc,
        }

    # ── Core computation ──────────────────────────────────────────────────────

    def _compute(self, rows: list) -> AdaptiveState:
        """Compute all adaptive parameters from signal rows."""
        # Sort by id desc (newest first)
        rows.sort(key=lambda r: r.get("id", 0), reverse=True)

        # 1. Rolling WR windows
        wr_50 = self._calc_wr(rows[:50])
        wr_100 = self._calc_wr(rows[:100])
        wr_200 = self._calc_wr(rows[:200])

        # WR by direction
        up_rows = [r for r in rows if r.get("direction") == "UP"]
        down_rows = [r for r in rows if r.get("direction") == "DOWN"]
        wr_up = self._calc_wr(up_rows[:100])
        wr_down = self._calc_wr(down_rows[:100])

        # 2. Adaptive threshold: best expectancy band
        optimal_threshold, best_label, best_exp = self._find_best_band(rows)

        # 3. Direction bias correction
        direction_bias_adj, bias_dir, bias_pct = self._calc_direction_bias(rows)

        # 4. Market regime
        regime, regime_adj, regime_size = self._detect_regime()

        # 5. Momentum factor
        wr_recent_10 = self._calc_wr(rows[:10])
        wr_baseline_50 = wr_50
        momentum = self._calc_momentum(wr_recent_10, wr_baseline_50)

        # 6. Calibration WR factor: scale confidence by how well the bot performs
        # If WR_50 > 50%: factor > 1 (boost). If < 50%: factor < 1 (reduce).
        if wr_50 is not None:
            calibration_wr_factor = _clamp(wr_50 / 0.50, 0.95, 1.05)
        else:
            calibration_wr_factor = 1.0

        # Composite effective threshold
        effective_threshold = optimal_threshold + regime_adj + direction_bias_adj
        effective_threshold = _clamp(effective_threshold, *_THRESHOLD_BOUNDS)

        return AdaptiveState(
            optimal_threshold=optimal_threshold,
            effective_threshold=effective_threshold,
            direction_bias_adj=direction_bias_adj,
            regime=regime, regime_adj=regime_adj, regime_size_factor=regime_size,
            momentum_factor=momentum, calibration_wr_factor=calibration_wr_factor,
            wr_50=wr_50, wr_100=wr_100, wr_200=wr_200,
            wr_up=wr_up, wr_down=wr_down,
            wr_recent_10=wr_recent_10, wr_baseline_50=wr_baseline_50,
            bias_direction=bias_dir, bias_pct=bias_pct,
            best_band_label=best_label, best_band_expectancy=best_exp,
            total_signals_used=len(rows),
            updated_at=time.time(),
        )

    @staticmethod
    def _calc_wr(rows: list) -> float | None:
        """Calculate win rate from rows with 'correct' field."""
        resolved = [r for r in rows if r.get("correct") is not None]
        if len(resolved) < _MIN_SIGNALS_PER_BAND:
            return None
        wins = sum(1 for r in resolved if r.get("correct"))
        return round(wins / len(resolved), 4)

    def _find_best_band(self, rows: list) -> tuple:
        """Find the confidence band with highest expectancy E."""
        best_threshold = _DEFAULT_THRESHOLD
        best_label = None
        best_exp = None

        for lo, hi in _CONF_BANDS:
            band_rows = [
                r for r in rows
                if r.get("confidence") is not None
                and lo <= float(r["confidence"]) < hi
                and r.get("correct") is not None
            ]
            if len(band_rows) < _MIN_SIGNALS_PER_BAND:
                continue

            wins = [r for r in band_rows if r.get("correct")]
            losses = [r for r in band_rows if not r.get("correct")]
            wr = len(wins) / len(band_rows)

            # Average win/loss in PnL terms (use pnl_pct if available, else 1:1)
            avg_win = self._avg_pnl(wins, default=0.001)
            avg_loss = abs(self._avg_pnl(losses, default=-0.001))
            if avg_loss == 0:
                avg_loss = 0.001

            expectancy = (wr * avg_win) - ((1 - wr) * avg_loss)

            if best_exp is None or expectancy > best_exp:
                best_exp = expectancy
                best_label = f"{lo:.2f}-{hi:.2f}"
                best_threshold = _clamp(lo, *_THRESHOLD_BOUNDS)

        return best_threshold, best_label, best_exp

    @staticmethod
    def _avg_pnl(rows: list, default: float = 0.0) -> float:
        """Average pnl_pct from rows, fallback to default."""
        pnls = [float(r["pnl_pct"]) for r in rows if r.get("pnl_pct") is not None]
        if not pnls:
            return default
        return sum(pnls) / len(pnls)

    def _calc_direction_bias(self, rows: list) -> tuple:
        """Detect directional bias in last N signals."""
        recent = rows[:_DIRECTION_WINDOW]
        if len(recent) < 10:
            return 0.0, None, None

        up_count = sum(1 for r in recent if r.get("direction") == "UP")
        down_count = sum(1 for r in recent if r.get("direction") == "DOWN")
        total = up_count + down_count
        if total < 10:
            return 0.0, None, None

        up_pct = up_count / total
        down_pct = down_count / total

        if up_pct >= _DIRECTION_BIAS_THRESHOLD:
            # Biased UP: make UP harder (+0.03), DOWN easier
            adj = _clamp(0.03, *_DIRECTION_ADJ_BOUNDS)
            return adj, "UP", round(up_pct, 3)
        elif down_pct >= _DIRECTION_BIAS_THRESHOLD:
            adj = _clamp(0.03, *_DIRECTION_ADJ_BOUNDS)
            return adj, "DOWN", round(down_pct, 3)

        return 0.0, None, None

    def _detect_regime(self) -> tuple:
        """Detect market regime via Binance 4h klines (ATR + trend)."""
        try:
            params = urllib.parse.urlencode({
                "symbol": "BTCUSDT", "interval": "4h", "limit": 22,
            })
            url = f"https://api.binance.com/api/v3/klines?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "btcbot-ace/1.0"})
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=8) as resp:
                klines = json.loads(resp.read().decode())

            if len(klines) < 16:
                return "UNKNOWN", 0.0, 1.0

            closes = [float(k[4]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]

            trs = []
            for i in range(1, len(klines)):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
                trs.append(tr)
            atr14 = sum(trs[-14:]) / 14 if len(trs) >= 14 else sum(trs) / max(len(trs), 1)
            atr_pct = (atr14 / closes[-1]) * 100.0 if closes[-1] > 0 else 0.0

            def _ema(vals, period):
                k = 2.0 / (period + 1)
                e = vals[0]
                for v in vals[1:]:
                    e = v * k + e * (1.0 - k)
                return e

            ema5 = _ema(closes[-5:], 5) if len(closes) >= 5 else closes[-1]
            ema20 = _ema(closes[-20:], 20) if len(closes) >= 20 else closes[-1]
            trend_strength = abs(ema5 - ema20) / ema20 * 100.0 if ema20 > 0 else 0.0

            if trend_strength > 0.5:
                return "TRENDING", _clamp(-0.02, *_REGIME_ADJ_BOUNDS), 1.0
            elif atr_pct > 1.5:
                return "VOLATILE", _clamp(0.0, *_REGIME_ADJ_BOUNDS), 0.5
            else:
                return "RANGING", _clamp(0.05, *_REGIME_ADJ_BOUNDS), 1.0

        except Exception:
            logger.debug("[ACE] regime detection failed — default UNKNOWN")
            return "UNKNOWN", 0.0, 1.0

    @staticmethod
    def _calc_momentum(wr_recent: float | None, wr_baseline: float | None) -> float:
        """Momentum factor: WR recent vs WR baseline."""
        if wr_recent is None or wr_baseline is None:
            return 1.0
        diff = wr_recent - wr_baseline  # positive = hot streak
        if diff >= 0.10:
            return 1.05  # hot streak bonus (within bounds by definition)
        elif diff <= -0.10:
            return 0.90  # cold streak penalty (intentionally below _MOMENTUM_BOUNDS floor)
        return 1.0

    # ── Data access ───────────────────────────────────────────────────────────

    def _fetch_signals(self) -> list:
        """Fetch last 200 resolved signals from Supabase."""
        if not self._sb_url or not self._sb_key:
            return []
        url = (
            f"{self._sb_url}/rest/v1/{self._table}"
            "?select=id,direction,confidence,correct,pnl_pct,pnl_usd,created_at"
            "&bet_taken=eq.true"
            "&correct=not.is.null"
            "&order=id.desc&limit=200"
        )
        req = urllib.request.Request(url, headers={
            "apikey": self._sb_key,
            "Authorization": f"Bearer {self._sb_key}",
        })
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as resp:
            return json.loads(resp.read().decode())

    def _persist_state(self, st: AdaptiveState, trigger: str, signals_used: int):
        """Save state to Supabase (fire-and-forget)."""
        if not self._sb_url or not self._sb_key:
            return
        try:
            state_payload = json.dumps({
                "id": 1,
                "signals_since_last_calc": 0,
                "total_signals_used": signals_used,
                "wr_50": st.wr_50, "wr_100": st.wr_100, "wr_200": st.wr_200,
                "wr_up": st.wr_up, "wr_down": st.wr_down,
                "optimal_threshold": st.optimal_threshold,
                "best_band_label": st.best_band_label,
                "best_band_expectancy": st.best_band_expectancy,
                "direction_bias_adj": st.direction_bias_adj,
                "bias_direction": st.bias_direction, "bias_pct": st.bias_pct,
                "regime": st.regime, "regime_adj": st.regime_adj,
                "regime_size_factor": st.regime_size_factor,
                "momentum_factor": st.momentum_factor,
                "wr_recent_10": st.wr_recent_10, "wr_baseline_50": st.wr_baseline_50,
                "effective_threshold": st.effective_threshold,
                "calibration_wr_factor": st.calibration_wr_factor,
            }).encode()
            req = urllib.request.Request(
                f"{self._sb_url}/rest/v1/bot_adaptive_state",
                data=state_payload,
                headers={
                    "apikey": self._sb_key,
                    "Authorization": f"Bearer {self._sb_key}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                method="POST",
            )
            urllib.request.urlopen(req, context=_SSL_CTX, timeout=5)

            # Log entry
            log_payload = json.dumps({
                "trigger_reason": trigger,
                "signals_used": signals_used,
                "optimal_threshold": st.optimal_threshold,
                "effective_threshold": st.effective_threshold,
                "direction_bias_adj": st.direction_bias_adj,
                "regime": st.regime, "regime_adj": st.regime_adj,
                "momentum_factor": st.momentum_factor,
                "calibration_wr_factor": st.calibration_wr_factor,
                "details": json.dumps(st.to_dict()),
            }).encode()
            log_req = urllib.request.Request(
                f"{self._sb_url}/rest/v1/bot_adaptive_log",
                data=log_payload,
                headers={
                    "apikey": self._sb_key,
                    "Authorization": f"Bearer {self._sb_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            urllib.request.urlopen(log_req, context=_SSL_CTX, timeout=5)
        except Exception:
            logger.debug("[ACE] persist_state failed", exc_info=True)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _passthrough(raw_conf: float, direction: str, reason: str = "passthrough") -> dict:
        """Return a no-adjustment result (fail-open)."""
        env_floor = float(os.environ.get("CONF_THRESHOLD", str(_DEFAULT_THRESHOLD)))
        return {
            "should_trade": raw_conf >= env_floor,
            "adjusted_conf": raw_conf,
            "raw_conf": raw_conf,
            "effective_threshold": env_floor,
            "optimal_threshold": _DEFAULT_THRESHOLD,
            "direction_bias_adj": 0.0,
            "regime": "UNKNOWN",
            "regime_adj": 0.0,
            "size_factor": 1.0,
            "momentum_factor": 1.0,
            "calibration_wr_factor": 1.0,
            "reason": reason,
        }
