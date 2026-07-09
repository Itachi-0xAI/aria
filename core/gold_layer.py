"""
Gold layer reader — supports local CSV (default) or public Google Sheets CSV export.
No extra dependencies: uses pandas.read_csv() for both paths.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).parent.parent.resolve()


def _config() -> dict:
    try:
        import yaml
        with open(_ROOT / "config" / "aria_config.yaml") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def read_gold(domain: str) -> pd.DataFrame:
    """
    Return the Gold layer DataFrame for *domain*.

    Resolution order:
    1. If aria_config.yaml → gold_layer.source == "sheets", fetch the public
       CSV-export URL for this domain (gold_layer.sheets.<domain>.url or
       built from spreadsheet_id + gid).
    2. Fall back to the local CSV at data/dksm/gold_layer/<domain>.csv.
    """
    cfg = _config()
    gl_cfg = cfg.get("gold_layer", {})
    source = gl_cfg.get("source", "csv")

    if source == "sheets":
        sheet_cfg = gl_cfg.get("sheets", {}).get(domain, {})
        url = sheet_cfg.get("url", "")
        if not url:
            spreadsheet_id = gl_cfg.get("spreadsheet_id", "")
            gid = sheet_cfg.get("gid", "0")
            if spreadsheet_id:
                url = (
                    f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
                    f"/export?format=csv&gid={gid}"
                )
        if url:
            try:
                return pd.read_csv(url)
            except Exception:
                pass  # fall through to local CSV

    # Local CSV fallback
    local_path = _ROOT / "data" / "dksm" / "gold_layer" / f"{domain}.csv"
    if local_path.exists():
        return pd.read_csv(local_path)

    # Try dksm config path as last resort
    try:
        import yaml
        domains_cfg_path = _ROOT / "config" / "dksm" / "domains.yaml"
        with open(domains_cfg_path) as f:
            domains = yaml.safe_load(f) or {}
        gold_path_str = domains.get(domain, {}).get("medallion", {}).get("gold", {}).get("path", "")
        if gold_path_str:
            return pd.read_csv(Path(gold_path_str))
    except Exception:
        pass

    return pd.DataFrame()
