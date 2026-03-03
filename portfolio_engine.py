"""
portfolio_engine.py — Portfolio-Aware Brain

Calculates a PortfolioState before every trade decision and replaces
the rigid same-dir/opposite-dir logic in place_bet() with a smarter
decision matrix that considers:

  - Current exposure and risk score
  - Unrealized P&L (absolute and %)
  - Recent WR and streak
  - Dynamic reverse thresholds (scale with loss magnitude)
  - Partial close + open (new action type)
  - Adaptive pyramid sizing

Fail-open: any exception → returns a FALLBACK decision that mirrors
the old logic so the bot never stops trading due to this module.

Kill switch: env PORTFOLIO_ENGINE_DISABLED=true → bypass entirely.
"""

import logging
import os
import time
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("portfolio_engine")

# ── Configurable thresholds (env vars for tuning without redeploy) ────────

# Pyramid
PYRAMID_RISK_CEIL = float(os.environ.get("PE_PYRAMID_RISK_CEIL", "40"))
PYRAMID_MIN_CONF = float(os.environ.get("PE_PYRAMID_MIN_CONF", "0.65"))
PYRAMID_MIN_PNL_PCT = float(os.environ.get("PE_PYRAMID_MIN_PNL", "0.0015"))  # 0.15%

# Reverse — dynamic threshold
REVERSE_BASE_THRESHOLD = float(os.environ.get("PE_REVERSE_BASE", "0.75"))
REVERSE_LOSS_SCALING = float(os.environ.get("PE_REVERSE_LOSS_SCALE", "0.1"))
REVERSE_FLOOR = float(os.environ.get("PE_REVERSE_FLOOR", "0.55"))

# Opposite direction — profit scenarios
PARTIAL_CLOSE_PROFIT_MIN = float(os.environ.get("PE_PARTIAL_PROFIT_MIN", "0.01"))    # 1%
PARTIAL_CLOSE_CONF_MIN = float(os.environ.get("PE_PARTIAL_CONF_MIN", "0.72"))
FULL_REVERSE_PROFIT_CONF = float(os.environ.get("PE_FULL_REVERSE_PROFIT_CONF", "0.78"))
FULL_REVERSE_PROFIT_MIN = float(os.environ.get("PE_FULL_REVERSE_PROFIT_MIN", "0.005"))  # 0.5%

# Partial close — loss scenario
PARTIAL_LOSS_CONF_MIN = float(os.environ.get("PE_PARTIAL_LOSS_CONF", "0.65"))
PARTIAL_LOSS_PNL_MIN = float(os.environ.get("PE_PARTIAL_LOSS_PNL", "0.005"))  # 0.5%

# Sizing
PYRAMID_SIZE_MIN_FACTOR = float(os.environ.get("PE_PYR_SIZE_MIN", "0.30"))
PYRAMID_SIZE_MAX_FACTOR = float(os.environ.get("PE_PYR_SIZE_MAX", "0.75"))
PARTIAL_NEW_SIZE_FACTOR = float(os.environ.get("PE_PARTIAL_SIZE", "0.50"))

# Max position hard cap (BTC)
MAX_POSITION_BTC = float(os.environ.get("PE_MAX_POSITION_BTC", "0.005"))


@dataclass
class PortfolioState:
    """Snapshot of the portfolio at decision time."""
    # Positions
    positions: list = field(default_factory=list)
    total_exposure_btc: float = 0.0
    total_exposure_pct: float = 0.0
    net_direction: str = "FLAT"         # LONG, SHORT, FLAT
    unrealized_pnl_usd: float = 0.0
    unrealized_pnl_pct: float = 0.0

    # Equity & risk
    equity: float = 0.0
    max_exposure_btc: float = MAX_POSITION_BTC
    risk_score: float = 0.0             # 0-100

    # Market context
    btc_price: float = 0.0
    regime: str = "UNKNOWN"

    # Recent performance
    wr_10: float = 50.0
    streak_count: int = 0
    streak_direction: str = ""          # "win" or "loss"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PortfolioDecision:
    """Output of evaluate_signal()."""
    action: str = "SKIP"                # OPEN, PYRAMID, REVERSE, PARTIAL_CLOSE_AND_OPEN, SKIP
    size: float = 0.0
    close_size: float = 0.0             # for partial close: how much to close
    reason: str = ""
    confidence_adjusted: float = 0.0
    risk_after: float = 0.0             # projected risk score after the action
    is_fallback: bool = False           # True if using legacy logic due to error

    def to_dict(self) -> dict:
        return asdict(self)


class PortfolioEngine:
    """Portfolio-aware decision engine for place_bet()."""

    def __init__(self):
        self.disabled = os.environ.get("PORTFOLIO_ENGINE_DISABLED", "").lower() in ("true", "1")

    def build_state(
        self,
        position: dict | None,          # from get_open_position(): {side, size, price} or None
        equity: float,
        btc_price: float,
        regime: str = "UNKNOWN",
        wr_10: float = 50.0,
        streak_count: int = 0,
        streak_direction: str = "",
        existing_pnl_pct: float = 0.0,  # pre-computed PnL % from place_bet()
        existing_entry_price: float = 0.0,
        pyramid_count: int = 0,
    ) -> PortfolioState:
        """Build a PortfolioState from the current Kraken + Supabase data."""
        state = PortfolioState(
            equity=equity,
            btc_price=btc_price,
            regime=regime,
            wr_10=wr_10,
            streak_count=streak_count,
            streak_direction=streak_direction,
            max_exposure_btc=MAX_POSITION_BTC,
        )

        if position is None:
            state.net_direction = "FLAT"
            return state

        pos_size = float(position.get("size", 0))
        pos_side = position.get("side", "").lower()
        pos_price = float(position.get("price", 0))

        state.positions = [{
            "side": pos_side,
            "size": pos_size,
            "price": pos_price,
            "pnl_pct": existing_pnl_pct,
            "pyramid_count": pyramid_count,
        }]
        state.total_exposure_btc = pos_size
        state.net_direction = "LONG" if pos_side == "long" else "SHORT"

        # Unrealized P&L
        if pos_price > 0 and btc_price > 0:
            _sign = 1 if pos_side == "long" else -1
            state.unrealized_pnl_usd = round((btc_price - pos_price) * pos_size * _sign, 6)
        state.unrealized_pnl_pct = existing_pnl_pct

        # Exposure as % of equity
        if equity > 0 and btc_price > 0:
            state.total_exposure_pct = round(pos_size * btc_price / equity * 100, 2)

        # Risk score
        state.risk_score = self._calculate_risk_score(state)

        return state

    def _calculate_risk_score(self, state: PortfolioState) -> float:
        """0 = safe, 100 = max risk.
        Calibrated for crypto futures: 1 position (~200% equity) = normal,
        max position (~400% equity) = high risk."""
        score = 0.0

        # Exposure factor (0-40 points): 200% = 20 pts, 400% = 40 pts
        score += min(40.0, state.total_exposure_pct * 0.1)

        # Unrealized loss factor (0-30 points)
        if state.unrealized_pnl_pct < 0:
            score += min(30.0, abs(state.unrealized_pnl_pct) * 100 * 15)  # -2% = 30 pts

        # Losing streak factor (0-20 points)
        if state.streak_direction == "loss" and state.streak_count >= 2:
            score += min(20.0, state.streak_count * 5.0)

        # WR decay factor (0-10 points)
        if state.wr_10 < 40:
            score += 10.0
        elif state.wr_10 < 50:
            score += 5.0

        return min(100.0, round(score, 1))

    def evaluate_signal(
        self,
        portfolio: PortfolioState,
        direction: str,                  # "UP" or "DOWN"
        confidence: float,
        xgb_prob_up: float,
        base_size: float,
    ) -> PortfolioDecision:
        """Core decision: what to do with this signal given the portfolio state.

        Returns a PortfolioDecision with action, size, and reason.
        """
        desired_side = "long" if direction == "UP" else "short"
        decision = PortfolioDecision(confidence_adjusted=confidence)

        # ── A) FLAT — no position ──
        if portfolio.net_direction == "FLAT":
            decision.action = "OPEN"
            decision.size = base_size
            decision.reason = "flat_new_position"
            decision.risk_after = self._project_risk(portfolio, base_size, direction)
            return decision

        current_side = portfolio.positions[0]["side"] if portfolio.positions else ""
        current_pnl_pct = portfolio.unrealized_pnl_pct
        current_size = portfolio.total_exposure_btc
        pyramid_count = portfolio.positions[0].get("pyramid_count", 0) if portfolio.positions else 0

        # ── B) SAME DIRECTION — pyramid or skip ──
        if current_side == desired_side:
            return self._evaluate_same_direction(
                portfolio, direction, confidence, xgb_prob_up,
                base_size, current_pnl_pct, current_size, pyramid_count,
            )

        # ── C) OPPOSITE DIRECTION — reverse, partial close, or skip ──
        return self._evaluate_opposite_direction(
            portfolio, direction, confidence, xgb_prob_up,
            base_size, current_pnl_pct, current_size,
        )

    def _evaluate_same_direction(
        self,
        portfolio: PortfolioState,
        direction: str,
        confidence: float,
        xgb_prob_up: float,
        base_size: float,
        current_pnl_pct: float,
        current_size: float,
        pyramid_count: int,
    ) -> PortfolioDecision:
        """Same direction: pyramid if conditions met, else skip."""
        decision = PortfolioDecision(confidence_adjusted=confidence)

        # Hard cap: only 1 pyramid allowed, total size <= MAX_POSITION_BTC
        pyramid_size = self._calculate_pyramid_size(base_size, confidence, current_pnl_pct)
        can_fit = (current_size + pyramid_size) <= MAX_POSITION_BTC
        can_pyramid = pyramid_count == 0 and can_fit

        if not can_pyramid:
            decision.action = "SKIP"
            if pyramid_count > 0:
                decision.reason = f"pyramid_already_done (count={pyramid_count})"
            else:
                decision.reason = f"position_too_large ({current_size + pyramid_size:.4f} > {MAX_POSITION_BTC})"
            return decision

        # Risk gate
        if portfolio.risk_score >= PYRAMID_RISK_CEIL:
            decision.action = "SKIP"
            decision.reason = f"risk_too_high ({portfolio.risk_score:.0f} >= {PYRAMID_RISK_CEIL})"
            return decision

        # Confidence gate
        if confidence <= PYRAMID_MIN_CONF:
            decision.action = "SKIP"
            decision.reason = f"confidence_too_low ({confidence:.2f} <= {PYRAMID_MIN_CONF})"
            return decision

        # PnL gate — but strong XGB signal can bypass
        xgb_directional = xgb_prob_up if direction == "UP" else (1.0 - xgb_prob_up)
        strong_xgb = xgb_directional > 0.70 and confidence > 0.72

        if current_pnl_pct <= PYRAMID_MIN_PNL_PCT and not strong_xgb:
            decision.action = "SKIP"
            decision.reason = (
                f"pnl_too_low ({current_pnl_pct*100:.2f}% <= {PYRAMID_MIN_PNL_PCT*100:.2f}%) "
                f"and no strong_xgb (xgb={xgb_directional:.2f}, conf={confidence:.2f})"
            )
            return decision

        # All gates passed → PYRAMID
        decision.action = "PYRAMID"
        decision.size = pyramid_size
        decision.reason = "strong_xgb_bypass" if strong_xgb else "standard_pyramid"
        decision.risk_after = self._project_risk(portfolio, pyramid_size, direction)
        return decision

    def _evaluate_opposite_direction(
        self,
        portfolio: PortfolioState,
        direction: str,
        confidence: float,
        xgb_prob_up: float,
        base_size: float,
        current_pnl_pct: float,
        current_size: float,
    ) -> PortfolioDecision:
        """Opposite direction: smart reverse/partial-close/skip."""
        decision = PortfolioDecision(confidence_adjusted=confidence)

        # ── Position in PROFIT ──
        if current_pnl_pct > 0:
            # High profit + decent confidence → partial close + open opposite
            if current_pnl_pct >= PARTIAL_CLOSE_PROFIT_MIN and confidence >= PARTIAL_CLOSE_CONF_MIN:
                close_size = round(current_size * 0.5, 6)
                new_size = round(base_size * PARTIAL_NEW_SIZE_FACTOR, 6)
                decision.action = "PARTIAL_CLOSE_AND_OPEN"
                decision.size = new_size
                decision.close_size = close_size
                decision.reason = (
                    f"profit_partial_close (pnl={current_pnl_pct*100:.2f}% >= {PARTIAL_CLOSE_PROFIT_MIN*100:.0f}%, "
                    f"conf={confidence:.2f})"
                )
                decision.risk_after = self._project_risk(portfolio, new_size, direction)
                return decision

            # Medium profit + very high confidence → full reverse
            if current_pnl_pct >= FULL_REVERSE_PROFIT_MIN and confidence >= FULL_REVERSE_PROFIT_CONF:
                decision.action = "REVERSE"
                decision.size = base_size
                decision.close_size = current_size
                decision.reason = (
                    f"profit_full_reverse (pnl={current_pnl_pct*100:.2f}% but conf={confidence:.2f} "
                    f">= {FULL_REVERSE_PROFIT_CONF})"
                )
                decision.risk_after = self._project_risk(portfolio, base_size, direction)
                return decision

            # Otherwise: skip — don't cut a winner
            decision.action = "SKIP"
            decision.reason = (
                f"preserve_profit (pnl={current_pnl_pct*100:.2f}%, conf={confidence:.2f} "
                f"insufficient for reverse)"
            )
            return decision

        # ── Position in LOSS ──
        # Dynamic reverse threshold: more loss → lower threshold
        abs_loss_pct = abs(current_pnl_pct)
        reverse_threshold = max(
            REVERSE_FLOOR,
            REVERSE_BASE_THRESHOLD - abs_loss_pct * 100 * REVERSE_LOSS_SCALING,
        )

        # Full reverse: confidence meets dynamic threshold
        if confidence >= reverse_threshold:
            decision.action = "REVERSE"
            decision.size = base_size
            decision.close_size = current_size
            decision.reason = (
                f"loss_reverse (loss={abs_loss_pct*100:.2f}%, threshold={reverse_threshold:.2f}, "
                f"conf={confidence:.2f})"
            )
            decision.risk_after = self._project_risk(portfolio, base_size, direction)
            return decision

        # Partial close + open: below reverse threshold but still decent signal & meaningful loss
        if (confidence >= PARTIAL_LOSS_CONF_MIN
                and abs_loss_pct >= PARTIAL_LOSS_PNL_MIN):
            close_size = round(current_size * 0.5, 6)
            new_size = round(base_size * PARTIAL_NEW_SIZE_FACTOR, 6)
            decision.action = "PARTIAL_CLOSE_AND_OPEN"
            decision.size = new_size
            decision.close_size = close_size
            decision.reason = (
                f"loss_partial_close (loss={abs_loss_pct*100:.2f}%, conf={confidence:.2f} "
                f"< reverse_threshold={reverse_threshold:.2f} but >= {PARTIAL_LOSS_CONF_MIN})"
            )
            decision.risk_after = self._project_risk(portfolio, new_size, direction)
            return decision

        # Skip
        decision.action = "SKIP"
        decision.reason = (
            f"opposite_skip (loss={abs_loss_pct*100:.2f}%, conf={confidence:.2f} "
            f"< reverse_threshold={reverse_threshold:.2f})"
        )
        return decision

    def _calculate_pyramid_size(
        self,
        base_size: float,
        confidence: float,
        pnl_pct: float,
    ) -> float:
        """Adaptive pyramid sizing: 30%-75% of base depending on confidence and PnL."""
        # Normalize confidence: 0.65-1.0 → 0-1
        conf_factor = max(0.0, min(1.0, (confidence - PYRAMID_MIN_CONF) / (1.0 - PYRAMID_MIN_CONF)))
        # Normalize profit: 0%-1% → 0-1
        profit_factor = max(0.0, min(1.0, pnl_pct / 0.01))
        # Combined factor: equal weight
        combined = conf_factor * 0.5 + profit_factor * 0.5
        pyr_pct = PYRAMID_SIZE_MIN_FACTOR + (PYRAMID_SIZE_MAX_FACTOR - PYRAMID_SIZE_MIN_FACTOR) * combined
        size = round(base_size * pyr_pct, 6)
        return max(0.001, size)  # Kraken minimum

    def _project_risk(
        self,
        portfolio: PortfolioState,
        added_size: float,
        direction: str,
    ) -> float:
        """Estimate the risk score after adding a position."""
        # Simple projection: add exposure
        new_exposure_btc = portfolio.total_exposure_btc + added_size
        new_exposure_pct = 0.0
        if portfolio.equity > 0 and portfolio.btc_price > 0:
            new_exposure_pct = new_exposure_btc * portfolio.btc_price / portfolio.equity * 100

        projected_score = 0.0
        projected_score += min(40.0, new_exposure_pct * 0.1)

        if portfolio.unrealized_pnl_pct < 0:
            projected_score += min(30.0, abs(portfolio.unrealized_pnl_pct) * 100 * 15)

        if portfolio.streak_direction == "loss" and portfolio.streak_count >= 2:
            projected_score += min(20.0, portfolio.streak_count * 5.0)

        if portfolio.wr_10 < 40:
            projected_score += 10.0
        elif portfolio.wr_10 < 50:
            projected_score += 5.0

        return min(100.0, round(projected_score, 1))
