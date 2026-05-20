"""
Company Profile Loader

Scans config/company_profiles/*.yaml and injects each company's custom domains
into a unified domain registry that the scorer, prober, and dashboard can use
without any changes to those modules.

Each company profile uses the same domain schema as domains.yaml, with two
extra fields per domain:
  - probe_schedule: hourly | daily | weekly | manual
  - notify_on_stale: bool

Usage:
    loader = CompanyProfileLoader()
    merged_cfg = loader.merged_config()          # drop-in replacement for domains.yaml config
    company_domains = loader.domains_for("retail_corp")
    all_custom = loader.all_custom_domains()     # {domain_key: (company_id, domain_cfg)}
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PROFILES_DIR = Path("config/company_profiles")
_BASE_CONFIG   = Path("config/domains.yaml")


class CompanyProfileLoader:
    """
    Loads company profile YAML files and merges their domains into the
    global domain registry.  Company-owned domains are tagged with
    ``_owner`` and ``_probe_schedule`` so the rest of the system can
    distinguish them from built-in domains.
    """

    def __init__(
        self,
        profiles_dir: str | Path = _PROFILES_DIR,
        base_config: str | Path = _BASE_CONFIG,
    ) -> None:
        self.profiles_dir = Path(profiles_dir)
        self.base_config  = Path(base_config)
        self._profiles: dict[str, dict] = {}   # company_id → full YAML
        self._load()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self.profiles_dir.exists():
            return
        for path in sorted(self.profiles_dir.glob("*.yaml")):
            try:
                with open(path) as f:
                    data = yaml.safe_load(f)
                if not data or "company" not in data:
                    continue
                cid = data["company"]["id"]
                self._profiles[cid] = data
                logger.info("Loaded company profile: %s (%s)", cid, path.name)
            except Exception as exc:
                logger.warning("Could not load company profile %s: %s", path, exc)

    def reload(self) -> None:
        self._profiles.clear()
        self._load()

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def company_ids(self) -> list[str]:
        return list(self._profiles.keys())

    def company_meta(self, company_id: str) -> dict:
        return self._profiles.get(company_id, {}).get("company", {})

    def domains_for(self, company_id: str) -> dict[str, dict]:
        """Return the domains dict for one company (annotated with owner metadata)."""
        raw = self._profiles.get(company_id, {}).get("domains", {})
        annotated = {}
        for key, cfg in raw.items():
            annotated[key] = {**cfg, "_owner": company_id, "_probe_schedule": cfg.get("probe_schedule", "manual")}
        return annotated

    def all_custom_domains(self) -> dict[str, tuple[str, dict]]:
        """
        Returns {domain_key: (company_id, domain_cfg)} for every domain
        across all loaded company profiles.  Domain keys are namespaced as
        ``{company_id}__{domain_key}`` to avoid collisions with built-in domains.
        """
        result: dict[str, tuple[str, dict]] = {}
        for cid in self.company_ids:
            for key, cfg in self.domains_for(cid).items():
                namespaced = f"{cid}__{key}"
                result[namespaced] = (cid, cfg)
        return result

    def merged_config(self) -> dict[str, Any]:
        """
        Return a config dict identical in structure to domains.yaml but with
        all company domains injected.  Safe to pass anywhere that expects the
        standard config dict.
        """
        with open(self.base_config) as f:
            cfg = yaml.safe_load(f)

        for namespaced_key, (cid, domain_cfg) in self.all_custom_domains().items():
            cfg["domains"][namespaced_key] = domain_cfg

        return cfg

    def domain_display_name(self, namespaced_key: str) -> str:
        """Return a human-readable label like 'RetailCorp — Coupon Rules'."""
        if "__" not in namespaced_key:
            return namespaced_key
        cid, _ = namespaced_key.split("__", 1)
        meta = self.company_meta(cid)
        company_label = meta.get("display_name", cid)
        parts = self._profiles.get(cid, {}).get("domains", {})
        local_key = namespaced_key.split("__", 1)[1]
        domain_label = parts.get(local_key, {}).get("display_name", local_key)
        return f"{company_label} — {domain_label}"

    def schedule_for(self, namespaced_key: str) -> str:
        if "__" not in namespaced_key:
            return "manual"
        cid, local = namespaced_key.split("__", 1)
        return self._profiles.get(cid, {}).get("domains", {}).get(local, {}).get("probe_schedule", "manual")
