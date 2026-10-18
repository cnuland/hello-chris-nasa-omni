"""Async HTTP client for inventory-api (contracts/openapi/inventory-api.yaml). Read-only: GET requests only."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class InventoryError(RuntimeError):
    """inventory-api unreachable or returned a 5xx / unexpected status (``status_code`` None when unreachable)."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class InventoryClient:
    """Thin read-only wrapper over inventory-api. Only GET is ever issued (the agent never writes; AC-AGT-5)."""

    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self.base_url, transport=transport, timeout=timeout, headers={"accept": "application/json"}
        )

    async def _get(self, path: str, params: Any = None, *, allow_404: bool = False) -> Any:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise InventoryError(f"inventory-api unreachable: {type(exc).__name__}") from exc
        if allow_404 and resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise InventoryError(
                f"inventory-api GET {path} returned HTTP {resp.status_code}", status_code=resp.status_code
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise InventoryError(f"inventory-api GET {path} returned invalid JSON") from exc

    @staticmethod
    def _seg(value: str) -> str:
        return quote(str(value), safe="")

    async def list_items(self, params: Any = None) -> dict[str, Any]:
        """GET /items -> {items, total}. ``params`` may be a dict or a list of (key, value) pairs."""
        return await self._get("/items", params)

    async def get_item(self, entity_id: str) -> dict[str, Any] | None:
        """GET /items/{id}; None on 404."""
        return await self._get(f"/items/{self._seg(entity_id)}", allow_404=True)

    async def item_history(self, entity_id: str) -> list[dict[str, Any]] | None:
        """GET /items/{id}/history; None on 404."""
        return await self._get(f"/items/{self._seg(entity_id)}/history", allow_404=True)

    async def list_events(self, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """GET /events."""
        return await self._get("/events", params)

    async def list_review(self, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """GET /review."""
        return await self._get("/review", params)

    async def search(self, q: str, limit: int = 20) -> list[dict[str, Any]]:
        """GET /search -> list of hits {type, id, snippet, time}."""
        return await self._get("/search", {"q": q, "limit": limit})

    async def stats(self) -> dict[str, Any]:
        """GET /stats."""
        return await self._get("/stats")

    async def catalog(self) -> list[dict[str, Any]]:
        """GET /catalog (AC-AGT-11)."""
        return await self._get("/catalog")

    async def locations(self) -> list[dict[str, Any]]:
        """GET /locations."""
        return await self._get("/locations")

    async def aclose(self) -> None:
        await self._client.aclose()
