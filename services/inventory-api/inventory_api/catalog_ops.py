"""Catalog insertion shared by POST /catalog and review new_catalog."""

from __future__ import annotations

from typing import Any

import psycopg

from .errors import Conflict, Unprocessable
from .repo import location_exists
from .schemas import CatalogItemIn

CATALOG_COLUMNS = (
    "catalog_id, class_name, display_name, synonyms, category, is_fungible, expected_count, "
    "home_location, reference_image_uri, created_at"
)


def insert_catalog_item(conn: psycopg.Connection, item: CatalogItemIn) -> dict[str, Any]:
    """Insert inside the caller's transaction. 409 on duplicate id, 422 on an unknown home_location."""
    if item.home_location is not None and not location_exists(conn, item.home_location):
        raise Unprocessable(f"unknown home_location {item.home_location!r}")
    row = conn.execute(
        f"""INSERT INTO catalog_item (catalog_id, class_name, display_name, synonyms, category, is_fungible,
                expected_count, home_location)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (catalog_id) DO NOTHING
            RETURNING {CATALOG_COLUMNS}""",
        (
            item.catalog_id,
            item.class_name,
            item.display_name,
            list(item.synonyms),
            item.category,
            item.is_fungible,
            item.expected_count,
            item.home_location,
        ),
    ).fetchone()
    if row is None:
        raise Conflict(f"catalog_id {item.catalog_id!r} already exists")
    return dict(row)
