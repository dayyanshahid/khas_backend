"""Load table/column business descriptions from data/schema_descriptions.json."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

_docs_cache: dict[str, Any] | None = None
_docs_cache_loaded_at: float = 0.0
_docs_cache_lock = threading.Lock()


def _table_key(name: str) -> str:
    """dbo.tblSales -> tblsales"""
    return name.rsplit(".", 1)[-1].strip().lower()


def _column_key(name: str) -> str:
    return name.strip().lower()


def schema_docs_path() -> Path:
    configured = getattr(settings, "CHATBOT_SCHEMA_DOCS_PATH", "")
    if configured:
        return Path(configured)
    return Path(settings.BASE_DIR) / "data" / "schema_descriptions.json"


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn JSON file shape into fast lookup dicts."""
    tables: dict[str, dict[str, str]] = {}
    columns: dict[str, dict[str, str]] = {}
    conventions: list[tuple[str, str]] = []

    for item in raw.get("conventions", []):
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            desc = str(item.get("description", "")).strip()
            if name and desc:
                conventions.append((name, desc))

    for table_name, meta in raw.get("tables", {}).items():
        if not isinstance(meta, dict):
            continue
        tkey = _table_key(str(table_name))
        tables[tkey] = {
            "name": str(table_name),
            "description": str(meta.get("description", "")).strip(),
        }
        col_map: dict[str, str] = {}
        for col_name, col_desc in meta.get("columns", {}).items():
            desc = str(col_desc).strip()
            if desc:
                col_map[_column_key(str(col_name))] = desc
        if col_map:
            columns[tkey] = col_map

    return {
        "ok": True,
        "application": str(raw.get("application", "")).strip(),
        "conventions": conventions,
        "tables": tables,
        "columns": columns,
    }


def load_schema_docs(*, force_refresh: bool = False) -> dict[str, Any]:
    """Load schema_descriptions.json. Cached in memory."""
    global _docs_cache, _docs_cache_loaded_at

    ttl = getattr(settings, "CHATBOT_SCHEMA_DOCS_CACHE_TTL", 3600)

    with _docs_cache_lock:
        if (
            not force_refresh
            and _docs_cache is not None
            and (time.time() - _docs_cache_loaded_at) < ttl
        ):
            return _docs_cache

        path = schema_docs_path()
        empty: dict[str, Any] = {
            "ok": False,
            "path": str(path),
            "application": "",
            "conventions": [],
            "tables": {},
            "columns": {},
            "error": None,
        }

        if not path.is_file():
            empty["error"] = f"Schema documentation file not found: {path}"
            logger.warning("[SchemaDocs] %s", empty["error"])
            _docs_cache = empty
            _docs_cache_loaded_at = time.time()
            return _docs_cache

        try:
            with path.open(encoding="utf-8") as f:
                raw = json.load(f)
            docs = _normalize(raw)
            docs["path"] = str(path)
            docs["error"] = None
        except Exception as exc:
            empty["error"] = str(exc)
            logger.exception("[SchemaDocs] failed to read %s", path)
            _docs_cache = empty
            _docs_cache_loaded_at = time.time()
            return _docs_cache

        _docs_cache = docs
        _docs_cache_loaded_at = time.time()
        logger.info(
            "[SchemaDocs] loaded from %s: %d tables, %d with column docs",
            path,
            len(docs["tables"]),
            len(docs["columns"]),
        )
        return _docs_cache


def column_name_from_catalog_entry(entry: str) -> str:
    """PKID (numeric(18,0)) -> PKID"""
    entry = entry.strip()
    if " (" in entry:
        return entry.split(" (", 1)[0].strip()
    return entry


def table_description(docs: dict[str, Any], object_name: str) -> str:
    return docs.get("tables", {}).get(_table_key(object_name), {}).get("description", "")


def column_description(docs: dict[str, Any], object_name: str, column_name: str) -> str:
    return docs.get("columns", {}).get(_table_key(object_name), {}).get(
        _column_key(column_name), ""
    )
