"""InventoryClient: the only way perception talks to inventory-api (HTTP, contracts/openapi/inventory-api.yaml)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

LIVE_STATUSES = ("confirmed", "provisional", "unknown")


class InventoryError(RuntimeError):
    """inventory-api answered with an error status (``status_code`` and ``detail`` attached)."""

    def __init__(self, message: str, status_code: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class InventoryClient(Protocol):
    def get_catalog(self) -> list[dict[str, Any]]: ...
    def get_locations(self) -> list[dict[str, Any]]: ...
    def candidates(
        self, embedding: Sequence[float], *, catalog_id: str | None = None, location_id: str | None = None, k: int = 10
    ) -> list[dict[str, Any]]: ...
    def post_window(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def count_live_entities(self, catalog_id: str) -> int: ...
    def list_items(self, **filters: Any) -> list[dict[str, Any]]: ...
    def patch_item(self, entity_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...
    def list_events(self, **filters: Any) -> list[dict[str, Any]]: ...


class HttpInventoryClient:
    """httpx implementation. ``http_client`` may be any httpx.Client (e.g. FastAPI's TestClient)."""

    def __init__(self, base_url: str, *, http_client: httpx.Client | None = None, timeout_s: float = 10.0):
        self.base_url = base_url
        self.http_client = http_client
        self.timeout_s = timeout_s
        self._own_client = http_client is None

    def _client(self) -> httpx.Client:
        if self.http_client is None:
            self.http_client = httpx.Client(timeout=self.timeout_s)
        return self.http_client

    def close(self) -> None:
        if self._own_client and self.http_client is not None:
            self.http_client.close()
            self.http_client = None

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = self.base_url.rstrip("/") + path
        try:
            resp = self._client().request(method, url, timeout=self.timeout_s, **kwargs)
        except httpx.HTTPError as exc:
            raise InventoryError(f"{method} {path} failed: {exc}") from exc
        if resp.status_code >= 400:
            try:
                detail: Any = resp.json()
            except ValueError:
                detail = resp.text[:500]
            logger.warning(
                "inventory-api error", extra={"method": method, "path": path, "status_code": resp.status_code}
            )
            raise InventoryError(
                f"{method} {path} -> HTTP {resp.status_code}", status_code=resp.status_code, detail=detail
            )
        if not resp.content:
            return None
        return resp.json()

    def get_catalog(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/catalog") or [])

    def get_locations(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/locations") or [])

    def candidates(
        self, embedding: Sequence[float], *, catalog_id: str | None = None, location_id: str | None = None, k: int = 10
    ) -> list[dict[str, Any]]:
        body = {
            "embedding": [float(v) for v in embedding],
            "catalog_id": catalog_id,
            "location_id": location_id,
            "k": int(k),
        }
        return list(self._request("POST", "/ingest/candidates", json=body) or [])

    def post_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /ingest/window; returns the 201 body; raises InventoryError on non-2xx."""
        return dict(self._request("POST", "/ingest/window", json=payload) or {})

    def count_live_entities(self, catalog_id: str) -> int:
        """Entities of the catalog class with status in confirmed/provisional/unknown (GET /items ... total)."""
        params = [("catalog_id", catalog_id), *[("status", st) for st in LIVE_STATUSES], ("limit", 1)]
        body = self._request("GET", "/items", params=params) or {}
        return int(body.get("total", 0))

    def list_items(self, **filters: Any) -> list[dict[str, Any]]:
        """GET /items with the given query filters; returns ``items``."""
        params: list[tuple[str, Any]] = []
        for key, value in filters.items():
            if value is None:
                continue
            if isinstance(value, list | tuple | set):
                params += [(key, v) for v in value]
            elif isinstance(value, bool):
                params.append((key, "true" if value else "false"))
            else:
                params.append((key, value))
        body = self._request("GET", "/items", params=params) or {}
        return list(body.get("items", []))

    def patch_item(self, entity_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return dict(self._request("PATCH", f"/items/{entity_id}", json=fields) or {})

    def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        """GET /events (entity_id, type, after, before, limit); newest first."""
        params = [(k, str(v)) for k, v in filters.items() if v is not None]
        return list(self._request("GET", "/events", params=params) or [])
