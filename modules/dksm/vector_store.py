"""
Medallion-aware vector store built on ChromaDB with hybrid BM25 re-ranking.

Collections created:
  gold_{domain}          — current entities (is_current=True)
  gold_{domain}_history  — all versions
  silver_{domain}        — silver layer (cleaned, validated)
  bronze_{domain}        — raw bronze layer (for lineage queries)
  probe_history          — all past probe results for drift detection

Embedding model: sentence-transformers/all-MiniLM-L6-v2 (local, no API cost).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import yaml

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# SearchResult dataclass
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    """Single result from a hybrid search across the medallion layers."""
    domain: str
    entity: str
    current_value: str
    effective_date: str
    similarity_score: float     # dense cosine similarity (0-1)
    bm25_score: float
    combined_score: float
    matched_document: str
    layer: str                  # gold | silver | bronze


# ---------------------------------------------------------------------------
# Lazy imports — avoid hard crash if chromadb/transformers not installed
# ---------------------------------------------------------------------------

def _import_chromadb():
    try:
        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        return chromadb, SentenceTransformerEmbeddingFunction
    except ImportError as exc:
        raise ImportError(
            "chromadb is required for the vector store. "
            "Run: pip install chromadb>=0.5.0 sentence-transformers>=2.7.0"
        ) from exc


def _import_bm25():
    try:
        from rank_bm25 import BM25Okapi
        return BM25Okapi
    except ImportError as exc:
        raise ImportError(
            "rank-bm25 is required. Run: pip install rank-bm25>=0.2.2"
        ) from exc


# ---------------------------------------------------------------------------
# Tokenizer for BM25
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9]+", text.lower())


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class MedallionVectorStore:
    """
    Hybrid RAG vector store across all medallion layers.

    On first run (empty chroma_db/), call initialize_collections() to embed
    all CSVs. Subsequent runs reuse the persisted ChromaDB.
    """

    _EMBEDDING_MODEL = "all-MiniLM-L6-v2"

    # Column mappings per domain (key_col, value_col)
    _DOMAIN_COLS: dict[str, tuple[str, str]] = {
        "customer_segments": ("segment_name", "min_annual_revenue_usd"),
        "product_catalog": ("product_name", "unit_price_usd"),
        "risk_thresholds": ("threshold_name", "threshold_value"),
    }

    # Silver column mappings (deduplicated silver CSVs may have different key cols)
    _SILVER_KEY_COLS: dict[str, str] = {
        "customer_segments": "segment_name",
        "product_catalog": "product_name",
        "risk_thresholds": "threshold_name",
    }

    def __init__(
        self,
        config_path: str = "config/domains.yaml",
        store_path: str = "data/chroma_db/",
    ) -> None:
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

        self.store_path = store_path
        self._client = None
        self._ef = None
        self._initialized = False

    # ------------------------------------------------------------------
    # Lazy ChromaDB client (avoids slow import at module load time)
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._client is None:
            chromadb, SentenceTransformerEmbeddingFunction = _import_chromadb()
            Path(self.store_path).mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=self.store_path)
            embedding_model = self.config.get("global_settings", {}).get(
                "embedding_model", self._EMBEDDING_MODEL
            )
            self._ef = SentenceTransformerEmbeddingFunction(model_name=embedding_model)
        return self._client

    def _get_or_create_collection(self, name: str):
        client = self._get_client()
        return client.get_or_create_collection(
            name=name,
            embedding_function=self._ef,
            metadata={"hnsw:space": "cosine"},
        )

    # ------------------------------------------------------------------
    # Collection initialization
    # ------------------------------------------------------------------

    def initialize_collections(
        self,
        gold_data_path: str = "data/gold_layer/",
        silver_data_path: str = "data/silver_layer/",
        bronze_data_path: str = "data/bronze_layer/",
        progress_callback=None,
    ) -> None:
        """
        Embed all medallion layer CSV rows into ChromaDB collections.
        Safe to call repeatedly — existing documents are upserted (not duplicated).

        progress_callback: optional callable(float) for Streamlit st.progress()
        """
        domains = list(self.config["domains"].keys())
        total_steps = len(domains) * 3  # gold + silver + bronze per domain
        step = 0

        for domain in domains:
            key_col, val_col = self._DOMAIN_COLS.get(domain, ("", ""))

            # ---------- Gold ----------
            gold_file = Path(gold_data_path) / f"{domain}.csv"
            if gold_file.exists():
                df = pd.read_csv(gold_file, dtype=str).fillna("")
                df_current = df[df["is_current"].str.lower() == "true"] if "is_current" in df.columns else df
                self._upsert_layer(f"gold_{domain}", df_current, domain, key_col, val_col, "gold")
                self._upsert_layer(f"gold_{domain}_history", df, domain, key_col, val_col, "gold_history")
                logger.info("Embedded gold_%s: %d current, %d total", domain, len(df_current), len(df))
            step += 1
            if progress_callback:
                progress_callback(step / total_steps)

            # ---------- Silver ----------
            silver_file = Path(silver_data_path) / f"{domain}_silver.csv"
            if silver_file.exists():
                df_s = pd.read_csv(silver_file, dtype=str).fillna("")
                s_key = self._SILVER_KEY_COLS.get(domain, key_col)
                self._upsert_layer(f"silver_{domain}", df_s, domain, s_key, val_col, "silver")
                logger.info("Embedded silver_%s: %d rows", domain, len(df_s))
            step += 1
            if progress_callback:
                progress_callback(step / total_steps)

            # ---------- Bronze ----------
            bronze_file = Path(bronze_data_path) / f"{domain}_bronze.csv"
            if bronze_file.exists():
                df_b = pd.read_csv(bronze_file, dtype=str).fillna("")
                b_key = self.config["domains"][domain]["medallion"]["bronze"].get(
                    "key_column", key_col
                )
                self._upsert_layer(f"bronze_{domain}", df_b, domain, b_key, "", "bronze")
                logger.info("Embedded bronze_%s: %d rows", domain, len(df_b))
            step += 1
            if progress_callback:
                progress_callback(step / total_steps)

        self._initialized = True
        logger.info("All medallion collections initialized.")

    def _upsert_layer(
        self,
        collection_name: str,
        df: pd.DataFrame,
        domain: str,
        key_col: str,
        val_col: str,
        layer: str,
    ) -> None:
        """Upsert all rows of a DataFrame into a named ChromaDB collection."""
        collection = self._get_or_create_collection(collection_name)
        documents, metadatas, ids = [], [], []

        for idx, (_, row) in enumerate(df.iterrows()):
            row_dict = row.to_dict()
            entity = row_dict.get(key_col, row_dict.get("segment_id", row_dict.get("product_id", row_dict.get("threshold_id", ""))))
            value = row_dict.get(val_col, "")
            version = row_dict.get("version", str(idx))

            doc = " | ".join(f"{k}: {v}" for k, v in row_dict.items() if v)
            doc_id = f"{layer}_{domain}_{_slugify(str(entity))}_{idx}"

            meta = {
                "domain": domain,
                "layer": layer,
                "entity": str(entity),
                "value": str(value),
                "effective_date": str(row_dict.get("effective_date", row_dict.get("validated_at", row_dict.get("ingested_at", "")))),
                "version": str(version),
                "is_current": str(row_dict.get("is_current", "true")),
            }

            documents.append(doc)
            metadatas.append(meta)
            ids.append(doc_id[:512])  # ChromaDB id length limit

        if documents:
            collection.upsert(documents=documents, metadatas=metadatas, ids=ids)

    # ------------------------------------------------------------------
    # Hybrid search
    # ------------------------------------------------------------------

    def hybrid_search(
        self,
        query: str,
        domain: str | None = None,
        top_k: int = 3,
        layer: str = "gold",        # gold | silver | bronze | all
    ) -> list[SearchResult]:
        """
        Two-stage hybrid retrieval.

        Stage 1 — Dense search via ChromaDB cosine similarity.
        Stage 2 — BM25 re-ranking of the top_k*2 candidates.
        Combined score = 0.6 * dense + 0.4 * bm25 (normalized).

        Always returns results — falls back to dense-only if BM25 fails.
        """
        client = self._get_client()
        cfg_weights = self.config.get("global_settings", {}).get(
            "hybrid_search_weights", {"dense": 0.6, "bm25": 0.4}
        )
        dense_w = cfg_weights.get("dense", 0.6)
        bm25_w = cfg_weights.get("bm25", 0.4)

        # Determine which collections to search
        collection_names = self._resolve_collections(domain, layer, client)
        if not collection_names:
            return []

        # Stage 1: dense search
        candidates: list[dict] = []
        for coll_name in collection_names:
            try:
                coll = client.get_collection(name=coll_name, embedding_function=self._ef)
                count = coll.count()
                if count == 0:
                    continue
                n_results = min(top_k * 2, count)
                results = coll.query(
                    query_texts=[query],
                    n_results=n_results,
                    include=["documents", "metadatas", "distances"],
                )
                for doc, meta, dist in zip(
                    results["documents"][0],
                    results["metadatas"][0],
                    results["distances"][0],
                ):
                    # ChromaDB cosine distance → similarity
                    sim = max(0.0, 1.0 - float(dist))
                    candidates.append({"doc": doc, "meta": meta, "dense_score": sim})
            except Exception as exc:
                logger.warning("Dense search failed for %s: %s", coll_name, exc)

        if not candidates:
            return []

        # Stage 2: BM25 re-ranking
        try:
            BM25Okapi = _import_bm25()
            corpus = [_tokenize(c["doc"]) for c in candidates]
            bm25 = BM25Okapi(corpus)
            bm25_raw = bm25.get_scores(_tokenize(query))

            # Normalize BM25 scores to [0, 1]
            bm25_max = max(bm25_raw) if max(bm25_raw) > 0 else 1.0
            bm25_norm = [s / bm25_max for s in bm25_raw]
        except Exception as exc:
            logger.warning("BM25 re-ranking failed, using dense only: %s", exc)
            bm25_norm = [0.0] * len(candidates)

        # Combine scores — apply per-entity boost from correction feedback
        for i, c in enumerate(candidates):
            c["bm25_score"] = bm25_norm[i]
            boost = float(c["meta"].get("boost", 1.0))
            c["combined_score"] = (dense_w * c["dense_score"] + bm25_w * bm25_norm[i]) * boost

        # Deduplicate by entity+domain, keep highest combined
        seen: dict[str, dict] = {}
        for c in candidates:
            key = f"{c['meta'].get('domain', '')}::{c['meta'].get('entity', '')}"
            if key not in seen or c["combined_score"] > seen[key]["combined_score"]:
                seen[key] = c

        sorted_candidates = sorted(seen.values(), key=lambda x: x["combined_score"], reverse=True)

        return [
            SearchResult(
                domain=c["meta"].get("domain", ""),
                entity=c["meta"].get("entity", ""),
                current_value=c["meta"].get("value", ""),
                effective_date=c["meta"].get("effective_date", ""),
                similarity_score=round(c["dense_score"], 4),
                bm25_score=round(c["bm25_score"], 4),
                combined_score=round(c["combined_score"], 4),
                matched_document=c["doc"],
                layer=c["meta"].get("layer", layer),
            )
            for c in sorted_candidates[:top_k]
        ]

    def boost_entity(self, domain: str, entity: str, boost: float = 1.5) -> None:
        """Increase retrieval weight for a corrected entity in Chroma metadata.
        Called by FLE after a correction is applied so the right value surfaces first.
        """
        try:
            col     = self._get_or_create_collection(f"gold_{domain}")
            results = col.get(
                where={"entity_name": entity},
                include=["metadatas", "ids"],
            )
            if results["ids"]:
                now = datetime.now(timezone.utc).isoformat()
                updated = [dict(m, boost=boost, boosted_at=now)
                           for m in results["metadatas"]]
                col.update(ids=results["ids"], metadatas=updated)
                logger.info("VS: boosted entity=%s domain=%s boost=%.1f", entity, domain, boost)
            else:
                logger.debug("VS: boost_entity — no records found for %s/%s", domain, entity)
        except Exception as exc:
            logger.warning("VS: boost_entity failed: %s", exc)

    def _resolve_collections(
        self,
        domain: str | None,
        layer: str,
        client,
    ) -> list[str]:
        """Return the list of collection names to search."""
        existing = {c.name for c in client.list_collections()}
        domains = [domain] if domain else list(self.config["domains"].keys())

        if layer == "all":
            layers = ["gold", "silver", "bronze"]
        elif layer == "gold":
            layers = ["gold"]
        elif layer == "silver":
            layers = ["gold", "silver"]
        else:
            layers = [layer]

        names = []
        for d in domains:
            for lyr in layers:
                if lyr == "gold":
                    name = f"gold_{d}"
                else:
                    name = f"{lyr}_{d}"
                if name in existing:
                    names.append(name)
        return names

    # ------------------------------------------------------------------
    # Probe history
    # ------------------------------------------------------------------

    def store_probe_result(self, probe) -> None:
        """
        Embed and store a ProbeResult in the 'probe_history' collection.
        Enables "find similar past probes" and drift pattern detection.
        """
        from src.prober import ProbeResult
        collection = self._get_or_create_collection("probe_history")

        doc = (
            f"domain:{probe.domain} entity:{probe.entity} "
            f"question:{probe.question} response:{probe.raw_response} "
            f"grade:{probe.crag_grade} level:{probe.staleness_level}"
        )
        doc_id = f"probe_{_slugify(probe.domain)}_{_slugify(probe.entity)}_{int(datetime.now(timezone.utc).timestamp())}"

        collection.upsert(
            documents=[doc],
            metadatas=[{
                "domain": probe.domain,
                "entity": probe.entity,
                "staleness_level": probe.staleness_level,
                "score": str(0.0),
                "crag_grade": probe.crag_grade,
                "timestamp": probe.timestamp,
                "question": probe.question[:500],
                "extracted_value": str(probe.extracted_value or ""),
            }],
            ids=[doc_id],
        )

    def find_similar_probes(
        self,
        query: str,
        days_back: int = 90,
    ) -> list[dict]:
        """
        Semantic search over probe_history collection.
        Filters by timestamp >= now - days_back.
        """
        try:
            client = self._get_client()
            existing = {c.name for c in client.list_collections()}
            if "probe_history" not in existing:
                return []

            collection = client.get_collection(
                name="probe_history", embedding_function=self._ef
            )
            if collection.count() == 0:
                return []

            results = collection.query(
                query_texts=[query],
                n_results=min(10, collection.count()),
                include=["documents", "metadatas", "distances"],
            )

            cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
            output = []
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                if meta.get("timestamp", "") >= cutoff:
                    output.append({
                        "domain": meta.get("domain", ""),
                        "entity": meta.get("entity", ""),
                        "question": meta.get("question", ""),
                        "staleness_level": meta.get("staleness_level", ""),
                        "crag_grade": meta.get("crag_grade", ""),
                        "timestamp": meta.get("timestamp", ""),
                        "similarity": round(1 - float(dist), 4),
                    })
            return output
        except Exception as exc:
            logger.warning("find_similar_probes failed: %s", exc)
            return []

    def _check_drift_trend(self, domain: str, entity: str, trajectory: list[dict]) -> None:
        """Emit a warning when the last 3 probe points show worsening staleness."""
        if len(trajectory) < 3:
            return
        recent = trajectory[-3:]
        sims = [p.get("semantic_similarity", 1.0) for p in recent]
        if sims[0] > sims[1] > sims[2]:
            logger.warning(
                "DRIFT ALERT — %s/%s: semantic_similarity falling over last 3 probes "
                "(%.3f → %.3f → %.3f). Entity knowledge is degrading.",
                domain, entity, sims[0], sims[1], sims[2],
            )

    def get_staleness_trajectory(self, domain: str, entity: str) -> list[dict]:
        """
        Return probe history for a specific entity over time.
        Used for the trend chart on Page 1 of the dashboard.
        """
        try:
            client = self._get_client()
            existing = {c.name for c in client.list_collections()}
            if "probe_history" not in existing:
                return []

            collection = client.get_collection(
                name="probe_history", embedding_function=self._ef
            )
            query = f"domain:{domain} entity:{entity}"
            results = collection.query(
                query_texts=[query],
                n_results=min(50, max(1, collection.count())),
                include=["metadatas", "distances"],
            )

            trajectory = []
            for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
                if meta.get("domain") == domain and entity.lower() in meta.get("entity", "").lower():
                    trajectory.append({
                        "date": meta.get("timestamp", "")[:10],
                        "semantic_similarity": round(1 - float(dist), 4),
                        "staleness_level": meta.get("staleness_level", "UNKNOWN"),
                    })
            result = sorted(trajectory, key=lambda x: x["date"])
            self._check_drift_trend(domain, entity, result)
            return result
        except Exception as exc:
            logger.warning("get_staleness_trajectory failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh_collections(
        self,
        progress_callback=None,
    ) -> None:
        """
        Re-embed all medallion layer CSVs from disk.
        Drops and recreates all domain collections for a clean refresh.
        Safe to call from the dashboard "Refresh Vector DB" button.
        """
        client = self._get_client()
        for collection in client.list_collections():
            name = collection.name
            if name != "probe_history":
                try:
                    client.delete_collection(name)
                    logger.info("Dropped collection: %s", name)
                except Exception:
                    pass

        cfg = self.config
        self.initialize_collections(
            gold_data_path="data/gold_layer/",
            silver_data_path="data/silver_layer/",
            bronze_data_path="data/bronze_layer/",
            progress_callback=progress_callback,
        )

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def is_initialized(self) -> bool:
        """Return True if at least one gold collection exists with documents."""
        try:
            client = self._get_client()
            collections = client.list_collections()
            for c in collections:
                if c.name.startswith("gold_") and not c.name.endswith("_history"):
                    coll = client.get_collection(name=c.name, embedding_function=self._ef)
                    if coll.count() > 0:
                        return True
            return False
        except Exception:
            return False

    def collection_stats(self) -> dict[str, int]:
        """Return {collection_name: document_count} for all collections."""
        try:
            client = self._get_client()
            stats = {}
            for c in client.list_collections():
                try:
                    coll = client.get_collection(name=c.name, embedding_function=self._ef)
                    stats[c.name] = coll.count()
                except Exception:
                    stats[c.name] = -1
            return stats
        except Exception:
            return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", text.lower())[:64]
