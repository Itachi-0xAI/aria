"""
Medallion architecture pipeline: Bronze → Silver → Gold.

Handles ingestion, validation, deduplication, and promotion
across all three layers for every DKSM domain.
"""

from __future__ import annotations

import csv
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class LayerStats:
    """Row counts and quality metrics for one layer of one domain."""
    domain: str
    layer: str          # bronze | silver | gold
    total_rows: int
    valid_rows: int
    duplicate_rows: int
    null_rows: int
    avg_quality_score: float
    last_updated: str


@dataclass
class PipelineRunResult:
    """Summary of a full Bronze → Silver → Gold pipeline run."""
    domain: str
    run_id: str
    started_at: str
    completed_at: str
    bronze_stats: LayerStats
    silver_stats: LayerStats
    gold_stats: LayerStats
    rows_promoted_to_silver: int
    rows_promoted_to_gold: int
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


# ---------------------------------------------------------------------------
# Quality checks
# ---------------------------------------------------------------------------

def _quality_score(row: dict[str, Any], required_columns: list[str]) -> float:
    """Return a 0-1 quality score for a single row."""
    if not required_columns:
        return 1.0
    filled = sum(
        1 for col in required_columns
        if col in row and row[col] is not None and str(row[col]).strip() != ""
    )
    return round(filled / len(required_columns), 4)


def _row_fingerprint(row: dict[str, Any], key_cols: list[str]) -> str:
    """Stable hash of key columns for deduplication."""
    key = "|".join(str(row.get(c, "")) for c in sorted(key_cols))
    return hashlib.md5(key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Bronze ingestion
# ---------------------------------------------------------------------------

class BronzeIngester:
    """Append-only raw ingestion. Adds provenance metadata, preserves originals."""

    def __init__(self, config: dict) -> None:
        self.config = config

    def ingest(self, domain: str, source_path: str) -> LayerStats:
        """
        Read source CSV and record row-level provenance.

        In a real pipeline this would stream from Kafka/S3; here it
        reads the pre-existing bronze CSVs that simulate raw arrivals.
        """
        bronze_cfg = self.config["domains"][domain]["medallion"]["bronze"]
        bronze_path = Path(bronze_cfg["path"])

        if not bronze_path.exists():
            raise FileNotFoundError(f"Bronze source not found: {bronze_path}")

        df = pd.read_csv(bronze_path, dtype=str)
        total = len(df)
        duplicates = int(df.get("is_duplicate", pd.Series(["false"] * total)).eq("true").sum())
        nulls = int(df.get("has_nulls", df.get("parse_error", pd.Series(["false"] * total))).eq("true").sum())

        return LayerStats(
            domain=domain,
            layer="bronze",
            total_rows=total,
            valid_rows=total - duplicates - nulls,
            duplicate_rows=duplicates,
            null_rows=nulls,
            avg_quality_score=round((total - duplicates - nulls) / max(total, 1), 4),
            last_updated=datetime.now(timezone.utc).isoformat(),
        )


# ---------------------------------------------------------------------------
# Silver refinery
# ---------------------------------------------------------------------------

class SilverRefinery:
    """
    Bronze → Silver transformation.
    Removes duplicates, repairs nulls, enforces types, scores quality.
    """

    # Required columns matched against bronze column names (pre-rename)
    _REQUIRED: dict[str, list[str]] = {
        "customer_segments": [
            "segment_name", "min_annual_revenue",
            "max_annual_revenue", "support_tier", "discount_rate_pct",
        ],
        "product_catalog": [
            "product_name", "category", "unit_price_usd",
            "discount_eligible", "launch_date",
        ],
        "risk_thresholds": [
            "threshold_name", "risk_category",
            "max_value", "unit", "severity_level",
        ],
    }

    # Bronze → Silver column renames per domain
    _RENAMES: dict[str, dict[str, str]] = {
        "customer_segments": {
            "min_annual_revenue": "min_annual_revenue_usd",
            "max_annual_revenue": "max_annual_revenue_usd",
        },
        "product_catalog": {},
        "risk_thresholds": {
            "max_value": "threshold_value",
        },
    }

    def __init__(self, config: dict) -> None:
        self.config = config

    def refine(self, domain: str) -> LayerStats:
        """
        Read bronze CSV, apply validation rules, write silver CSV.
        Returns stats about what was promoted.
        """
        bronze_cfg = self.config["domains"][domain]["medallion"]["bronze"]
        silver_cfg = self.config["domains"][domain]["medallion"]["silver"]
        quality_threshold = float(silver_cfg.get("quality_threshold", 0.95))

        bronze_path = Path(bronze_cfg["path"])
        silver_path = Path(silver_cfg["path"])

        df_bronze = pd.read_csv(bronze_path, dtype=str)

        # Remove rows flagged as duplicates or having parse errors
        dup_col = "is_duplicate" if "is_duplicate" in df_bronze.columns else None
        err_col = next((c for c in ["has_nulls", "parse_error"] if c in df_bronze.columns), None)

        mask_valid = pd.Series([True] * len(df_bronze))
        if dup_col:
            mask_valid &= df_bronze[dup_col].str.lower() != "true"
        if err_col:
            mask_valid &= df_bronze[err_col].str.lower() != "true"

        df_clean = df_bronze[mask_valid].copy()

        # Score quality per row
        required_cols = self._REQUIRED.get(domain, [])
        df_clean["quality_score"] = df_clean.apply(
            lambda r: _quality_score(r.to_dict(), required_cols), axis=1
        )
        df_clean["validated_at"] = datetime.now(timezone.utc).isoformat()

        # Filter below quality threshold
        df_valid = df_clean[df_clean["quality_score"] >= quality_threshold].copy()

        # Apply column renames (bronze → silver canonical names)
        renames = self._RENAMES.get(domain, {})
        if renames:
            df_valid = df_valid.rename(columns=renames)

        silver_path.parent.mkdir(parents=True, exist_ok=True)
        df_valid.to_csv(silver_path, index=False)

        stats = LayerStats(
            domain=domain,
            layer="silver",
            total_rows=len(df_valid),
            valid_rows=len(df_valid),
            duplicate_rows=int((~mask_valid & (df_bronze.get(dup_col, pd.Series()) == "true")).sum()) if dup_col else 0,
            null_rows=int((~mask_valid & (df_bronze.get(err_col, pd.Series()) == "true")).sum()) if err_col else 0,
            avg_quality_score=float(df_valid["quality_score"].mean()) if len(df_valid) else 0.0,
            last_updated=datetime.now(timezone.utc).isoformat(),
        )
        logger.info("Silver refinery [%s]: %d rows promoted", domain, stats.total_rows)
        return stats


# ---------------------------------------------------------------------------
# Gold curator
# ---------------------------------------------------------------------------

class GoldCurator:
    """
    Silver → Gold promotion.
    Keeps only is_current=True from the pre-existing gold CSVs (which are
    the authoritative warehouse snapshots). In production this would run
    an aggregation SQL; here it just reads the existing gold CSVs and
    validates their structure.
    """

    def __init__(self, config: dict) -> None:
        self.config = config

    def curate(self, domain: str) -> LayerStats:
        """
        Read gold CSV, verify required columns, return quality stats.
        The gold CSVs are already the output of the aggregation step.
        """
        gold_cfg = self.config["domains"][domain]["medallion"]["gold"]
        gold_path = Path(gold_cfg["path"])

        if not gold_path.exists():
            raise FileNotFoundError(f"Gold layer not found: {gold_path}")

        df = pd.read_csv(gold_path, dtype=str)
        if "is_current" not in df.columns:
            raise ValueError(
                f"Gold layer for domain '{domain}' is missing required column 'is_current'. "
                f"Schema contract violated — check {gold_path}"
            )
        is_current = df["is_current"].str.lower() == "true"
        df_current = df[is_current]

        return LayerStats(
            domain=domain,
            layer="gold",
            total_rows=len(df),
            valid_rows=len(df_current),
            duplicate_rows=0,
            null_rows=int(df.isnull().any(axis=1).sum()),
            avg_quality_score=1.0,
            last_updated=datetime.now(timezone.utc).isoformat(),
        )

    def get_current_entities(self, domain: str) -> pd.DataFrame:
        """Return only current (is_current=True) rows for a domain."""
        gold_cfg = self.config["domains"][domain]["medallion"]["gold"]
        df = pd.read_csv(gold_cfg["path"], dtype=str)
        if "is_current" in df.columns:
            df = df[df["is_current"].str.lower() == "true"]
        return df.reset_index(drop=True)

    def get_entity_value(self, domain: str, entity_name: str) -> dict[str, str] | None:
        """
        Fetch a single current entity's row as a dict.
        entity_name is matched against the gold key_column.
        """
        gold_cfg = self.config["domains"][domain]["medallion"]["gold"]
        key_col = gold_cfg["key_column"]
        df = self.get_current_entities(domain)
        match = df[df[key_col].str.lower() == entity_name.lower()]
        if match.empty:
            # fuzzy: contains
            match = df[df[key_col].str.lower().str.contains(entity_name.lower(), na=False)]
        return match.iloc[0].to_dict() if not match.empty else None

    def get_lineage(self, domain: str, entity_name: str) -> list[dict]:
        """Return full version history for an entity across Gold layer."""
        gold_cfg = self.config["domains"][domain]["medallion"]["gold"]
        key_col = gold_cfg["key_column"]
        df = pd.read_csv(gold_cfg["path"], dtype=str)
        history = df[df[key_col].str.lower().str.contains(entity_name.lower(), na=False)]
        return history.to_dict(orient="records")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class MedallionPipeline:
    """
    Orchestrates full Bronze → Silver → Gold pipeline for all domains.
    Call run() to execute the full pipeline; call run_domain() for one domain.
    """

    def __init__(self, config_path: str = "config/domains.yaml") -> None:
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

        self.ingester = BronzeIngester(self.config)
        self.refinery = SilverRefinery(self.config)
        self.curator = GoldCurator(self.config)

    def run_domain(self, domain: str) -> PipelineRunResult:
        """Run full pipeline for a single domain."""
        run_id = f"run_{domain}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        started = datetime.now(timezone.utc).isoformat()
        errors: list[str] = []

        try:
            bronze_stats = self.ingester.ingest(domain, "")
        except Exception as exc:
            errors.append(f"Bronze ingestion failed: {exc}")
            bronze_stats = LayerStats(domain, "bronze", 0, 0, 0, 0, 0.0, "")

        try:
            silver_stats = self.refinery.refine(domain)
        except Exception as exc:
            errors.append(f"Silver refinery failed: {exc}")
            silver_stats = LayerStats(domain, "silver", 0, 0, 0, 0, 0.0, "")

        try:
            gold_stats = self.curator.curate(domain)
        except Exception as exc:
            errors.append(f"Gold curation failed: {exc}")
            gold_stats = LayerStats(domain, "gold", 0, 0, 0, 0, 0.0, "")

        return PipelineRunResult(
            domain=domain,
            run_id=run_id,
            started_at=started,
            completed_at=datetime.now(timezone.utc).isoformat(),
            bronze_stats=bronze_stats,
            silver_stats=silver_stats,
            gold_stats=gold_stats,
            rows_promoted_to_silver=silver_stats.total_rows,
            rows_promoted_to_gold=gold_stats.valid_rows,
            errors=errors,
        )

    def run_all(self) -> list[PipelineRunResult]:
        """Run pipeline for every configured domain."""
        return [self.run_domain(d) for d in self.config["domains"]]

    def get_layer_summary(self) -> dict[str, dict[str, LayerStats]]:
        """
        Return layer stats for every domain without running the full pipeline.
        Useful for the dashboard sidebar.
        """
        summary: dict[str, dict[str, LayerStats]] = {}
        for domain in self.config["domains"]:
            try:
                bronze = self.ingester.ingest(domain, "")
                silver = self.refinery.refine(domain)
                gold = self.curator.curate(domain)
                summary[domain] = {"bronze": bronze, "silver": silver, "gold": gold}
            except Exception as exc:
                logger.warning("Could not compute stats for %s: %s", domain, exc)
        return summary
