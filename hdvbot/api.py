"""Client de l'API publique NationsGlory (https://publicapi.nationsglory.fr/v1)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable
from urllib.parse import quote, urljoin

import aiohttp

log = logging.getLogger("hdvbot.api")

ENVELOPE_KEYS = ("data", "items", "results", "trades", "records", "rows")


class ApiError(Exception):
    pass


class NotFound(ApiError):
    pass


def unwrap(payload: Any) -> tuple[Any, dict]:
    """Sépare les données de l'enveloppe ({"data": ..., "meta": ...})."""
    if isinstance(payload, list):
        return payload, {}
    if isinstance(payload, dict):
        for k in ENVELOPE_KEYS:
            if k in payload and isinstance(payload[k], (list, dict)):
                meta = {kk: vv for kk, vv in payload.items() if kk != k}
                for mk in ("meta", "pagination", "paging", "links"):
                    if isinstance(meta.get(mk), dict):
                        meta = {**meta, **meta[mk]}
                return payload[k], meta
        return payload, {}
    return payload, {}


def find_next(meta: dict) -> str | None:
    if meta.get("has_more") is False or meta.get("hasMore") is False:
        return None
    for k in ("next_cursor", "nextCursor", "next", "next_page", "next_url", "cursor"):
        v = meta.get(k)
        if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v) not in ("", "None", "null"):
            if k == "cursor" and not (meta.get("has_more") or meta.get("hasMore")):
                continue
            return str(v)
    return None


def is_stale(meta: dict) -> bool:
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if "stale" in k.lower() and v is True:
                    return True
                if walk(v):
                    return True
        elif isinstance(x, list):
            return any(walk(v) for v in x)
        return False
    return walk(meta)


class NGApi:
    def __init__(self, base_url: str, server: str, api_key: str | None = None,
                 delay: float = 0.5, page_size: int = 100):
        self.base = base_url.rstrip("/")
        self.server = server
        self.api_key = api_key or None
        self.delay = delay
        self.page_size = page_size
        self._session: aiohttp.ClientSession | None = None
        self._last = 0.0
        self.last_meta: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _headers(self) -> dict:
        h = {"Accept": "application/json", "User-Agent": "hdvbot/1.0 (bot Discord perso)"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    async def _throttle(self):
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last = time.monotonic()

    def url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base}{path}"

    async def request(self, path: str, params: dict | None = None) -> Any:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        url = self.url(path)
        last_err = None
        for attempt in range(4):
            await self._throttle()
            try:
                async with self._session.get(url, params=params, headers=self._headers()) as r:
                    if r.status == 429:
                        wait = float(r.headers.get("Retry-After") or 30)
                        log.warning("API : trop de requêtes (429), pause %.0fs", wait)
                        await asyncio.sleep(min(wait, 300))
                        continue
                    if r.status >= 500:
                        last_err = ApiError(f"{r.status} sur {url}")
                        await asyncio.sleep(2 ** attempt * 2)
                        continue
                    if r.status in (401, 403):
                        body = (await r.text())[:200]
                        raise ApiError(f"{r.status} : clé API manquante ou refusée ({url}). "
                                       f"Vérifie nationsglory.api_key. Réponse : {body}")
                    if r.status == 404:
                        raise NotFound(f"404 sur {url}")
                    if r.status >= 400:
                        body = (await r.text())[:300]
                        raise ApiError(f"{r.status} sur {url} : {body}")
                    return await r.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
                last_err = ApiError(f"{type(e).__name__} sur {url} : {e}")
                await asyncio.sleep(2 ** attempt * 2)
        raise last_err or ApiError(f"Échec répété sur {url}")

    async def get(self, path: str, params: dict | None = None) -> tuple[Any, dict]:
        data, meta = unwrap(await self.request(path, params))
        self.last_meta = meta
        if is_stale(meta):
            log.warning("API : données marquées périmées (stale) pour %s", path)
        return data, meta

    async def paginate(self, path: str, params: dict | None = None, max_pages: int = 100,
                       stop: Callable[[list], bool] | None = None) -> list[dict]:
        params = dict(params or {})
        params.setdefault("limit", self.page_size)
        url = self.url(path)
        out: list[dict] = []
        seen_first: set[str] = set()
        offset = 0
        for _ in range(max_pages):
            data, meta = await self.get(url, params)
            if not isinstance(data, list) or not data:
                break
            fp = json.dumps(data[0], sort_keys=True, default=str)
            if fp in seen_first:      # l'API ignore notre paramètre de pagination
                break
            seen_first.add(fp)
            out.extend(x for x in data if isinstance(x, dict))
            if stop and stop(data):
                break
            nxt = find_next(meta)
            if nxt:
                if nxt.startswith(("http", "/", "?")):
                    url, params = urljoin(url, nxt), None
                else:
                    params = {**(params or {}), "cursor": nxt}
                continue
            if meta.get("has_more") is False or len(data) < int((params or {}).get("limit", self.page_size)):
                break
            offset += len(data)
            params = {**(params or {}), "offset": offset}
        return out

    # --- Endpoints HDV ---------------------------------------------------------
    def _p(self, suffix: str) -> str:
        return f"/servers/{self.server}/hdv{suffix}"

    async def items(self, max_pages: int = 100) -> list[dict]:
        return await self.paginate(self._p("/items"), max_pages=max_pages)

    async def item(self, item_id: str) -> dict:
        data, _ = await self.get(self._p(f"/items/{quote(item_id, safe=':')}"))
        if isinstance(data, list):
            data = data[0] if data and isinstance(data[0], dict) else {}
        return data if isinstance(data, dict) else {}

    async def trades(self, item: str | None = None, limit: int = 100, max_pages: int = 1,
                     stop: Callable[[list], bool] | None = None) -> list[dict]:
        params: dict = {"limit": limit}
        if item:
            params["item"] = item
        return await self.paginate(self._p("/trades"), params, max_pages=max_pages, stop=stop)

    async def series(self, item_id: str, rng: str = "30d") -> Any:
        data, _ = await self.get(self._p(f"/items/{quote(item_id, safe=':')}/series"), {"range": rng})
        return data

    async def variants(self, item_id: str) -> Any:
        data, _ = await self.get(self._p(f"/items/{quote(item_id, safe=':')}/variants"))
        return data
