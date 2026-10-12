"""Thin client for the CollegeFootballData API (collegefootballdata.com).

Get a free key at https://collegefootballdata.com/key and either:
  - set the CFBD_API_KEY environment variable, or
  - save it to cfb-ratings/data/api_key.txt (gitignored by nature of this
    being a non-git folder; don't commit it if you ever add git), or
  - pass --api-key on the CLI.

All responses are cached to disk under data/cache/ so repeated runs during
development don't hammer the API or burn rate limits.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://api.collegefootballdata.com"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = DATA_DIR / "cache"
KEY_FILE = DATA_DIR / "api_key.txt"


def resolve_api_key(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env_key = os.environ.get("CFBD_API_KEY")
    if env_key:
        return env_key
    if KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
        if key:
            return key
    raise RuntimeError(
        "No CollegeFootballData API key found. Get a free one at "
        "https://collegefootballdata.com/key, then either set the "
        "CFBD_API_KEY environment variable or save it to "
        f"{KEY_FILE}"
    )


class CfbdClient:
    def __init__(self, api_key: str | None = None, use_cache: bool = True):
        self.api_key = resolve_api_key(api_key)
        self.use_cache = use_cache
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        )
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, endpoint: str, params: dict[str, Any]) -> Path:
        key_raw = endpoint + "?" + json.dumps(params, sort_keys=True)
        digest = hashlib.sha256(key_raw.encode()).hexdigest()[:24]
        safe_endpoint = endpoint.strip("/").replace("/", "_")
        return CACHE_DIR / f"{safe_endpoint}_{digest}.json"

    def get(self, endpoint: str, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        cache_path = self._cache_path(endpoint, params)
        if self.use_cache and cache_path.exists():
            return json.loads(cache_path.read_text())

        resp = self.session.get(f"{BASE_URL}{endpoint}", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        cache_path.write_text(json.dumps(data))
        return data

    # --- Specific endpoints used by the rating model -----------------

    def teams(self, year: int) -> list[dict]:
        return self.get("/teams/fbs", year=year)

    def fcs_teams(self, year: int) -> list[dict]:
        all_teams = self.get("/teams", year=year)
        return [t for t in all_teams if t.get("classification") == "fcs"]

    def games(self, year: int, season_type: str = "both", week: int | None = None) -> list[dict]:
        return self.get("/games", year=year, seasonType=season_type, week=week)

    def talent(self, year: int) -> list[dict]:
        return self.get("/talent", year=year)

    def srs(self, year: int) -> list[dict]:
        return self.get("/ratings/srs", year=year)
