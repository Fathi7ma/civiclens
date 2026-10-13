"""Socrata Open Data API client.

Design notes
------------
* Every read is recorded so the UI can show the **exact reproducible query**
  that produced an answer. That URL is the product: a judge can click any
  citation and independently verify the claim.
* No API key is required for these datasets. A token is optional and only
  raises rate limits.
* This Austin instance does not expose ``within_circle`` / ``within_distance``,
  so geometry narrowing uses ``within_box`` (coarse, server-side) followed by
  precise local filtering. See ``geo.py``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests

from .config import app_token, cache_ttl, dataset, http_timeout

log = logging.getLogger("civiclens.socrata")

USER_AGENT = "CivicLens/1.0 (open civic data answer engine)"


class SocrataError(RuntimeError):
    """Raised when the upstream open-data API fails.

    The engine converts this into an honest refusal rather than a guess.
    """

    def __init__(self, message: str, status: Optional[int] = None, query_url: str = ""):
        super().__init__(message)
        self.status = status
        self.query_url = query_url


class _TTLCache:
    """Thread-safe TTL cache with a disk-backed tier.

    Judges re-run demos constantly and the upstream portal is slow enough that a
    cold cache is visible in a live presentation. Persisting results to disk
    means restarting the server does not reset the demo's responsiveness.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._d: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._dirty = False
        self._stop = threading.Event()
        self.path = path
        if self.path:
            self._load()
            self._flusher = threading.Thread(target=self._flush_loop, daemon=True)
            self._flusher.start()

    # ------------------------------------------------------------- memory --
    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            hit = self._d.get(key)
            if not hit:
                return None
            expires, value = hit
            if expires < time.time():
                self._d.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        ttl = cache_ttl() if ttl is None else ttl
        with self._lock:
            self._d[key] = (time.time() + ttl, value)
            self._dirty = True

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self._dirty = True

    def __len__(self) -> int:
        with self._lock:
            return len(self._d)

    # -------------------------------------------------------------- disk --
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            return
        now = time.time()
        kept = {k: (e, v) for k, (e, v) in blob.get("entries", {}).items() if e > now}
        with self._lock:
            self._d.update(kept)

    def flush(self) -> None:
        if not self.path:
            return
        with self._lock:
            if not self._dirty:
                return
            snapshot = dict(self._d)
            self._dirty = False
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"saved_at": time.time(), "entries": snapshot}, fh)
            os.replace(tmp, self.path)
        except OSError as exc:  # cache persistence is best-effort
            log.debug("cache flush failed: %s", exc)

    def _flush_loop(self) -> None:
        while not self._stop.wait(5.0):
            self.flush()

    def close(self) -> None:
        self._stop.set()
        self.flush()


def _default_cache_path() -> str:
    root = os.getenv("CIVICLENS_CACHE_DIR")
    if not root:
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.join(os.path.dirname(here), "data", "cache")
    return os.path.join(root, "queries.json")


CACHE = _TTLCache(path=_default_cache_path())


def _encode(params: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Socrata params are passed through verbatim; requests handles encoding."""
    out: Dict[str, str] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            value = ",".join(str(v) for v in value)
        out[key] = str(value)
    return out


class SocrataClient:
    """Thin, citation-first client over the Socrata SODA API."""

    def __init__(
        self,
        domain: str,
        token: Optional[str] = None,
        timeout: Optional[float] = None,
        retries: int = 3,
    ) -> None:
        self.domain = domain
        self.token = token
        self.timeout = timeout if timeout is not None else http_timeout()
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        #: number of upstream calls made this process (surfaced in /api/health)
        self.call_count = 0

    # ------------------------------------------------------------------ urls
    def build_url(self, dataset_id: str, params: Optional[Dict[str, Any]] = None) -> str:
        """Build the reproducible API URL for a query.

        ``$``, parentheses and commas are left unescaped so the citation reads
        as the SoQL query that produced it. Percent-encoded ``%24select=``
        works just as well but is unreadable in a citation panel, and these
        URLs are shown to people on purpose.
        """
        qs = urllib.parse.urlencode(_encode(params), safe="$(),':*")
        base = f"https://{self.domain}/resource/{dataset_id}.json"
        return f"{base}?{qs}" if qs else base

    def cite_url(self, dataset_key: str, **params: Any) -> str:
        ds = dataset(dataset_key)
        return self.build_url(ds.id, params)

    # ----------------------------------------------------------------- calls
    def _get(self, dataset_id: str, params: Dict[str, str]) -> Tuple[List[Dict[str, Any]], str]:
        url = self.build_url(dataset_id, params)
        headers: Dict[str, str] = {}
        token = self.token or app_token()
        if token:
            headers["X-App-Token"] = token

        cached = CACHE.get(url)
        if cached is not None:
            return cached, url

        last_err: Optional[str] = None
        for attempt in range(self.retries):
            try:
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
                self.call_count += 1
                if resp.status_code == 200:
                    payload = resp.json()
                    rows = payload if isinstance(payload, list) else [payload]
                    CACHE.set(url, rows)
                    return rows, url
                # 400 means a bad query - retrying will not help.
                if resp.status_code == 400:
                    raise SocrataError(
                        "Open-data API rejected the query: %s"
                        % (resp.json().get("message", resp.text)[:200]),
                        status=400,
                        query_url=url,
                    )
                last_err = "HTTP %s" % resp.status_code
            except SocrataError:
                raise
            except requests.RequestException as exc:
                last_err = str(exc)
            if attempt < self.retries - 1:
                time.sleep(0.4 * (2 ** attempt))
        raise SocrataError("Open-data API unreachable after retries: %s" % last_err, query_url=url)

    # ------------------------------------------------------------ operations
    def query(
        self,
        dataset_key: str,
        select: Optional[str] = None,
        where: Optional[str] = None,
        order: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        extra: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"$limit": limit}
        if select:
            params["$select"] = select
        if where:
            params["$where"] = where
        if order:
            params["$order"] = order
        if offset:
            params["$offset"] = offset
        if extra:
            params.update(extra)
        rows, _ = self._get(dataset(dataset_key).id, _encode(params))
        return rows

    def query_with_url(
        self,
        dataset_key: str,
        select: Optional[str] = None,
        where: Optional[str] = None,
        order: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """Same as :meth:`query` but also returns the reproducible query URL.

        Callers that build citations need the URL, and deriving it separately
        risks the citation drifting from the query that actually ran.
        """
        params: Dict[str, Any] = {"$limit": limit}
        if select:
            params["$select"] = select
        if where:
            params["$where"] = where
        if order:
            params["$order"] = order
        rows, url = self._get(dataset(dataset_key).id, _encode(params))
        return rows, url

    def aggregate(
        self,
        dataset_key: str,
        select: str,
        group: Optional[str] = None,
        order: Optional[str] = None,
        where: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Group-by aggregation, e.g. ``count(*) as n group by district``."""
        params: Dict[str, Any] = {"$select": select, "$limit": limit}
        if group:
            params["$group"] = group
        if order:
            params["$order"] = order
        if where:
            params["$where"] = where
        rows, _ = self._get(dataset(dataset_key).id, _encode(params))
        return rows

    def count(self, dataset_key: str, where: Optional[str] = None) -> int:
        params: Dict[str, Any] = {"$select": "count(1) as n"}
        if where:
            params["$where"] = where
        rows, _ = self._get(dataset(dataset_key).id, _encode(params))
        if rows and isinstance(rows[0].get("n"), (int, str)):
            return int(rows[0]["n"])
        return 0

    def fields(self, dataset_key: str) -> List[Dict[str, Any]]:
        ds = dataset(dataset_key)
        url = f"https://{self.domain}/api/views/{ds.id}"
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        meta = resp.json()
        return [
            {"name": c.get("name"), "field": c.get("fieldName"), "type": c.get("dataTypeName")}
            for c in meta.get("columns", [])
        ]

    def box(
        self,
        dataset_key: str,
        geom_field: str,
        bbox: Tuple[float, float, float, float],
        select: Optional[str] = None,
        where_extra: Optional[str] = None,
        limit: int = 500,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """Server-side coarse spatial filter via ``within_box``.

        ``within_box(geom, min_lat, min_lon, max_lat, max_lon)``.
        Precise filtering happens locally - see ``geo.point_in_geom``.
        """
        min_lat, min_lon, max_lat, max_lon = bbox
        where = "within_box(%s, %s, %s, %s, %s)" % (
            geom_field,
            min_lat,
            min_lon,
            max_lat,
            max_lon,
        )
        if where_extra:
            where = "(%s) and (%s)" % (where, where_extra)
        rows = self.query(dataset_key, select=select, where=where, limit=limit)
        url = self.cite_url(dataset_key, **{"$select": select, "$where": where, "$limit": limit})
        return rows, url


def _client_lock() -> threading.Lock:
    global _LOCK
    try:
        return _LOCK
    except NameError:
        _LOCK = threading.Lock()
        return _LOCK


_LOCK = threading.Lock()
_CLIENT: Optional[SocrataClient] = None


def get_client() -> SocrataClient:
    """Process-wide singleton client."""
    global _CLIENT
    from .config import CITY

    with _client_lock():
        if _CLIENT is None:
            _CLIENT = SocrataClient(domain=str(CITY["domain"]), token=app_token())
        return _CLIENT