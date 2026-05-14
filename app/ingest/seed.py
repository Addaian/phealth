"""
Seed-data loader for the ingestion pipeline.

The ASAM dimension catalog and the TJC Element-of-Performance catalog are
reference *fixtures*, not code -- they live as YAML under ``data/seed/`` so a
clinician can review or extend them without touching Python. This module loads
and caches them.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# data/seed/ lives at the repo root: app/ingest/seed.py -> parents[2] is the root.
_SEED_DIR = Path(__file__).resolve().parents[2] / "data" / "seed"


@lru_cache
def load_seed(filename: str) -> dict[str, Any]:
    """Load and cache a YAML seed file from ``data/seed/``."""
    with (_SEED_DIR / filename).open(encoding="utf-8") as seed_file:
        return yaml.safe_load(seed_file)
