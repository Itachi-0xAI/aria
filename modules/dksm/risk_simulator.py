"""
Risk simulator: estimates financial exposure from stale domain knowledge.

Maps each stale entity to the volume and average cost of decisions that
rely on it, then computes an expected loss over a configurable window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import yaml

from src.scorer import StalenessScore

logger = logging.getLogger(__name__)


@dataclass
class RiskExposure:
    """Financial risk exposure for one stale entity."""
    domain: str
    entity: str
    staleness_level: str
    staleness_score: float
    decisions_in_window: int
    wrong_decision_rate: float      # estimated fraction that will be wrong
    avg_cost_per_wrong_decision_usd: float
    estimated_exposure_usd: float
    decisions_at_risk: int
    window_days: int
    calculation_timestamp: str


@dataclass
class PortfolioRisk:
    """Aggregated risk across all stale entities."""
    total_exposure_usd: float
    total_decisions_at_risk: int
    critical_entities: list[str]
    stale_entities: list[str]
    domain_breakdown: dict[str, float]
    risk_grade: str          # A | B | C | D | F
    calculation_timestamp: str


# ---------------------------------------------------------------------------
# Wrong-decision rate model
# ---------------------------------------------------------------------------

_WRONG_RATE: dict[str, float] = {
    "CRITICAL": 0.65,
    "STALE": 0.30,
    "FRESH": 0.02,
    "UNKNOWN": 0.50,
}


def _wrong_decision_rate(staleness_level: str, staleness_score: float) -> float:
    """Blend the categorical rate with the continuous score for finer estimates."""
    base = _WRONG_RATE.get(staleness_level, 0.30)
    return round(base * (0.7 + 0.3 * staleness_score), 4)


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

class RiskSimulator:
    """
    Estimates financial exposure from stale domain knowledge.
    Uses decision_log.csv as historical baseline for decision volume.
    """

    def __init__(
        self,
        config_path: str = "config/domains.yaml",
        decision_log_path: str = "data/decision_log.csv",
    ) -> None:
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

        self.decision_log: pd.DataFrame | None = None
        try:
            self.decision_log = pd.read_csv(decision_log_path)
            self.decision_log["timestamp"] = pd.to_datetime(self.decision_log["timestamp"], utc=True)
        except Exception as exc:
            logger.warning("Could not load decision log: %s", exc)

    def _monthly_decision_volume(self, domain: str) -> int:
        """Decisions per month from config (fallback: 100)."""
        return self.config["domains"].get(domain, {}).get(
            "business_impact", {}
        ).get("decisions_per_month", 100)

    def _avg_cost(self, domain: str) -> float:
        """Average cost per wrong decision from config."""
        impact = self.config["domains"].get(domain, {}).get("business_impact", {})
        for key in impact:
            if "cost" in key.lower():
                return float(impact[key])
        return 50_000.0

    def simulate_entity(
        self,
        score: StalenessScore,
        window_days: int = 30,
    ) -> RiskExposure:
        """Compute exposure for a single stale entity."""
        monthly_vol = self._monthly_decision_volume(score.domain)
        decisions_in_window = int(monthly_vol * window_days / 30)
        wrong_rate = _wrong_decision_rate(score.staleness_level, score.staleness_score)
        decisions_at_risk = int(decisions_in_window * wrong_rate)
        avg_cost = self._avg_cost(score.domain)
        exposure = round(decisions_at_risk * avg_cost, 2)

        return RiskExposure(
            domain=score.domain,
            entity=score.entity,
            staleness_level=score.staleness_level,
            staleness_score=score.staleness_score,
            decisions_in_window=decisions_in_window,
            wrong_decision_rate=wrong_rate,
            avg_cost_per_wrong_decision_usd=avg_cost,
            estimated_exposure_usd=exposure,
            decisions_at_risk=decisions_at_risk,
            window_days=window_days,
            calculation_timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def simulate_portfolio(
        self,
        scores: list[StalenessScore],
        window_days: int = 30,
    ) -> PortfolioRisk:
        """Compute aggregate portfolio risk from all staleness scores."""
        exposures = [self.simulate_entity(s, window_days) for s in scores]
        total_exposure = sum(e.estimated_exposure_usd for e in exposures)
        total_at_risk = sum(e.decisions_at_risk for e in exposures)

        domain_breakdown: dict[str, float] = {}
        for e in exposures:
            domain_breakdown[e.domain] = round(
                domain_breakdown.get(e.domain, 0.0) + e.estimated_exposure_usd, 2
            )

        critical = [e.entity for e in exposures if e.staleness_level == "CRITICAL"]
        stale = [e.entity for e in exposures if e.staleness_level == "STALE"]

        # Risk grade based on total exposure
        if total_exposure < 100_000:
            grade = "A"
        elif total_exposure < 500_000:
            grade = "B"
        elif total_exposure < 1_000_000:
            grade = "C"
        elif total_exposure < 5_000_000:
            grade = "D"
        else:
            grade = "F"

        return PortfolioRisk(
            total_exposure_usd=round(total_exposure, 2),
            total_decisions_at_risk=total_at_risk,
            critical_entities=critical,
            stale_entities=stale,
            domain_breakdown=domain_breakdown,
            risk_grade=grade,
            calculation_timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def historical_exposure(
        self,
        domain: str | None = None,
        days_back: int = 90,
    ) -> pd.DataFrame:
        """
        Compute realized exposure from decision_log.csv for historical analysis.
        Returns a DataFrame grouped by domain and week with total risk_amount_usd.
        """
        if self.decision_log is None:
            raise FileNotFoundError(
                "Decision log not loaded — cannot compute historical exposure. "
                "Ensure decision_log_path points to a readable CSV file."
            )

        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        df = self.decision_log[self.decision_log["timestamp"] >= cutoff].copy()

        if domain:
            df = df[df["domain"] == domain]

        if df.empty:
            return pd.DataFrame()

        df["week"] = df["timestamp"].dt.to_period("W").dt.start_time
        return (
            df.groupby(["week", "domain"])["risk_amount_usd"]
            .sum()
            .reset_index()
            .rename(columns={"risk_amount_usd": "realized_exposure_usd"})
        )
