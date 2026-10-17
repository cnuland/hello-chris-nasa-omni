"""agent_memory access (memory tier 3, "dreaming" output). Advisory only; inventory facts come from SQL/API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

SCOPES = ("entity", "location", "global")

UPSERT_SQL = """
INSERT INTO agent_memory (scope, scope_key, summary, priority, source_from, source_to, generated_at)
VALUES (%s, %s, %s, %s, %s, %s, now())
ON CONFLICT (scope, scope_key) DO UPDATE SET
    summary = EXCLUDED.summary, priority = EXCLUDED.priority, source_from = EXCLUDED.source_from,
    source_to = EXCLUDED.source_to, generated_at = EXCLUDED.generated_at
"""


def check_scope(scope: str) -> None:
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}, got {scope!r}")


class MemoryStore:
    """Synchronous psycopg access to ``agent_memory`` (one short connection per call)."""

    def __init__(self, database_url: str):
        self.database_url = database_url

    def get(self, scope: str, key: str) -> dict[str, Any] | None:
        """Row as dict {scope, scope_key, summary, priority, source_from, source_to, generated_at} or None."""
        check_scope(scope)
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            row = conn.execute(
                """SELECT scope, scope_key, summary, priority, source_from, source_to, generated_at
                   FROM agent_memory WHERE scope = %s AND scope_key = %s""",
                (scope, key),
            ).fetchone()
        return dict(row) if row else None

    def get_global(self) -> str | None:
        """Summary text of the ('global','global') row, or None."""
        row = self.get("global", "global")
        return row["summary"] if row else None

    def upsert(
        self,
        scope: str,
        key: str,
        summary: str,
        *,
        priority: float = 0.5,
        source_from: datetime | None = None,
        source_to: datetime | None = None,
    ) -> None:
        check_scope(scope)
        with psycopg.connect(self.database_url) as conn:
            conn.execute(UPSERT_SQL, (scope, key, summary, priority, source_from, source_to))
