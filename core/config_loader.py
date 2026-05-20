"""
ARIA Config Loader
Loads aria_config.yaml + pipeline_map.yaml and merges with DKSM domains.yaml.
Generates install_id on first run. Single source of truth for all module config.
"""
from __future__ import annotations

import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).parent.parent
_ARIA_CFG  = _ROOT / "config" / "aria_config.yaml"
_PIPE_MAP  = _ROOT / "config" / "pipeline_map.yaml"
_DKSM_CFG  = _ROOT / "config" / "dksm" / "domains.yaml"


class ConfigLoader:
    """Unified config for all ARIA modules."""

    def __init__(self) -> None:
        self._aria  = self._load(_ARIA_CFG)
        self._pipe  = self._load(_PIPE_MAP)
        self._dksm  = self._load(_DKSM_CFG) if _DKSM_CFG.exists() else {}
        self._ensure_install_id()

    # ------------------------------------------------------------------
    def _load(self, path: Path) -> dict:
        with open(path) as f:
            return yaml.safe_load(f) or {}

    def _ensure_install_id(self) -> None:
        if not self._aria.get("aria", {}).get("install_id"):
            self._aria.setdefault("aria", {})["install_id"] = str(uuid.uuid4())
            with open(_ARIA_CFG, "w") as f:
                yaml.dump(self._aria, f, default_flow_style=False)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def demo_mode(self) -> bool:
        return bool(self._aria.get("aria", {}).get("demo_mode", True))

    @property
    def install_id(self) -> str:
        return self._aria["aria"]["install_id"]

    @property
    def version(self) -> str:
        return self._aria.get("aria", {}).get("version", "1.0.0")

    def module(self, name: str) -> dict[str, Any]:
        """Return config dict for a module (lci, pp, avl, fle, asgc, dksm)."""
        return self._aria.get(name, {})

    def module_enabled(self, name: str) -> bool:
        return bool(self.module(name).get("enabled", True))

    @property
    def pipeline_mappings(self) -> list[dict]:
        return self._pipe.get("mappings", [])

    def domain_for_model(self, dbt_model: str) -> dict | None:
        """Return the pipeline mapping entry for a dbt model name."""
        for m in self.pipeline_mappings:
            if m.get("dbt_model") == dbt_model:
                return m
        return None

    @property
    def dksm_domains(self) -> dict[str, Any]:
        return self._dksm.get("domains", {})

    @property
    def dksm_globals(self) -> dict[str, Any]:
        return self._dksm.get("global_settings", {})

    @property
    def staleness_thresholds(self) -> dict[str, int]:
        return self._aria.get("staleness_thresholds", {
            "fresh_days": 30, "stale_days": 90, "critical_days": 180
        })

    def asgc_lead(self) -> str:
        return self._aria.get("asgc", {}).get("lead_name", "")

    def set_lead(self, name: str) -> None:
        self._aria.setdefault("asgc", {})["lead_name"] = name
        with open(_ARIA_CFG, "w") as f:
            yaml.dump(self._aria, f, default_flow_style=False)

    def approval_required_for(self) -> list[str]:
        return self._aria.get("asgc", {}).get("approval_required_for", [])


@lru_cache(maxsize=1)
def get_config() -> ConfigLoader:
    """Cached singleton — safe to call from any module."""
    return ConfigLoader()
