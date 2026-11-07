"""SQLite persistence for station definitions."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import Station

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    url           TEXT    NOT NULL,
    slug          TEXT    NOT NULL UNIQUE,
    codec         TEXT    NOT NULL DEFAULT 'aac',
    on_demand     INTEGER NOT NULL DEFAULT 0,
    desired_state INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL
);
"""

UPDATABLE_COLUMNS = {"name", "url", "codec", "on_demand"}


def _row_to_station(row: sqlite3.Row) -> Station:
    return Station(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        slug=row["slug"],
        codec=row["codec"],
        on_demand=bool(row["on_demand"]),
        desired_state=bool(row["desired_state"]),
        created_at=row["created_at"],
    )


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(stations)")
            }
            if "on_demand" not in columns:
                connection.execute(
                    "ALTER TABLE stations "
                    "ADD COLUMN on_demand INTEGER NOT NULL DEFAULT 0"
                )
            if "codec" not in columns:
                connection.execute(
                    "ALTER TABLE stations "
                    "ADD COLUMN codec TEXT NOT NULL DEFAULT 'aac'"
                )

            # Older iterations had MP3, low-bitrate AAC and 64k transcode
            # modes. The current app is AAC-copy only, and HE-AAC copy is
            # always-on. Map any old rows into the supported combinations.
            connection.execute(
                "UPDATE stations SET codec = 'aac_he' "
                "WHERE codec = 'aac_low' AND on_demand = 0"
            )
            connection.execute(
                "UPDATE stations SET codec = 'aac' "
                "WHERE codec NOT IN ('aac', 'aac_he')"
            )
            connection.execute(
                "UPDATE stations SET codec = 'aac' "
                "WHERE codec = 'aac_he' AND on_demand = 1"
            )

    def list_stations(self) -> list[Station]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM stations ORDER BY id ASC"
            ).fetchall()
        return [_row_to_station(row) for row in rows]

    def get_station(self, station_id: int) -> Station | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM stations WHERE id = ?", (station_id,)
            ).fetchone()
        return _row_to_station(row) if row else None

    def get_station_by_slug(self, slug: str) -> Station | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM stations WHERE slug = ?", (slug,)
            ).fetchone()
        return _row_to_station(row) if row else None

    def unique_slug(self, base: str) -> str:
        with self.connect() as connection:
            candidate = base
            suffix = 2
            while connection.execute(
                "SELECT 1 FROM stations WHERE slug = ?", (candidate,)
            ).fetchone():
                tail = f"-{suffix}"
                candidate = f"{base[: 48 - len(tail)]}{tail}"
                suffix += 1
        return candidate

    def create_station(
        self,
        *,
        name: str,
        url: str,
        slug: str,
        codec: str,
        on_demand: bool,
        desired_state: bool,
    ) -> Station:
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO stations (
                    name, url, slug, codec, on_demand, desired_state, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    url,
                    slug,
                    codec,
                    int(on_demand),
                    int(desired_state),
                    created_at,
                ),
            )
            station_id = int(cursor.lastrowid)
            row = connection.execute(
                "SELECT * FROM stations WHERE id = ?", (station_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - defensive
            raise RuntimeError("Failed to read the station that was just created.")
        return _row_to_station(row)

    def update_station(self, station_id: int, **updates: object) -> Station | None:
        fields = {
            key: value
            for key, value in updates.items()
            if key in UPDATABLE_COLUMNS and value is not None
        }
        if not fields:
            return self.get_station(station_id)

        assignments = ", ".join(f"{column} = ?" for column in fields)
        values = [*fields.values(), station_id]
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE stations SET {assignments} WHERE id = ?", values
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM stations WHERE id = ?", (station_id,)
            ).fetchone()
        return _row_to_station(row) if row else None

    def set_desired_state(self, station_id: int, desired: bool) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE stations SET desired_state = ? WHERE id = ?",
                (int(desired), station_id),
            )
        return cursor.rowcount > 0

    def delete_station(self, station_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM stations WHERE id = ?", (station_id,)
            )
        return cursor.rowcount > 0

    def delete_all_stations(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM stations")
        return cursor.rowcount
