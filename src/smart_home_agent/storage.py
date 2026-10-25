"""SQLite persistence for the mock device fleet and personalization rules."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


DEVICE_SEEDS = (
    {
        "device_id": "ac_living",
        "name": "Living Room AC",
        "type": "air_conditioner",
        "room": "Living Room",
        "capabilities": {
            "power": True,
            "temperature": {"min": 16, "max": 30, "unit": "°C"},
            "modes": ["Cool", "Eco", "Sleep", "Fan", "Dry"],
        },
        "state": {"power": False, "temperature": 24, "mode": "Cool"},
    },
    {
        "device_id": "ac_bedroom",
        "name": "Bedroom AC",
        "type": "air_conditioner",
        "room": "Bedroom",
        "capabilities": {
            "power": True,
            "temperature": {"min": 16, "max": 30, "unit": "°C"},
            "modes": ["Cool", "Eco", "Sleep", "Fan", "Dry"],
        },
        "state": {"power": False, "temperature": 26, "mode": "Cool"},
    },
    {
        "device_id": "fan_living",
        "name": "Living Room Fan",
        "type": "fan",
        "room": "Living Room",
        "capabilities": {
            "power": True,
            "fan_speed": {"min": 1, "max": 5},
        },
        "state": {"power": False, "fan_speed": 1},
    },
    {
        "device_id": "fan_bedroom",
        "name": "Bedroom Fan",
        "type": "fan",
        "room": "Bedroom",
        "capabilities": {
            "power": True,
            "fan_speed": {"min": 1, "max": 5},
        },
        "state": {"power": False, "fan_speed": 1},
    },
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class SQLiteStore:
    """Small persistence boundary; each operation owns its SQLite connection."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    device_type TEXT NOT NULL,
                    room TEXT NOT NULL,
                    capabilities TEXT NOT NULL,
                    state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rules (
                    rule_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('default', 'phrase', 'time')),
                    trigger_phrase TEXT,
                    after_time TEXT,
                    actions TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS rules_enabled_kind
                    ON rules (enabled, kind, updated_at);
                """
            )
            device_count = connection.execute(
                "SELECT COUNT(*) FROM devices"
            ).fetchone()[0]
            if device_count == 0:
                connection.executemany(
                    """
                    INSERT INTO devices
                        (device_id, name, device_type, room, capabilities, state)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            device["device_id"],
                            device["name"],
                            device["type"],
                            device["room"],
                            json.dumps(device["capabilities"]),
                            json.dumps(device["state"]),
                        )
                        for device in DEVICE_SEEDS
                    ],
                )

    @staticmethod
    def _device(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "device_id": row["device_id"],
            "name": row["name"],
            "type": row["device_type"],
            "room": row["room"],
            "capabilities": json.loads(row["capabilities"]),
            "state": json.loads(row["state"]),
        }

    @staticmethod
    def _rule(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "rule_id": row["rule_id"],
            "name": row["name"],
            "kind": row["kind"],
            "trigger_phrase": row["trigger_phrase"],
            "after_time": row["after_time"],
            "actions": json.loads(row["actions"]),
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_devices(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM devices ORDER BY room, device_type, device_id"
            ).fetchall()
        return [self._device(row) for row in rows]

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
        return self._device(row) if row else None

    def update_device_states(self, states: dict[str, dict[str, Any]]) -> None:
        with self._connect() as connection:
            for device_id, state in states.items():
                result = connection.execute(
                    "UPDATE devices SET state = ? WHERE device_id = ?",
                    (json.dumps(state), device_id),
                )
                if result.rowcount != 1:
                    raise ValueError(f"Unknown device: {device_id}")

    def create_rule(
        self,
        *,
        name: str,
        kind: str,
        trigger_phrase: str | None,
        after_time: str | None,
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = _now()
        rule_id = str(uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO rules
                    (rule_id, name, kind, trigger_phrase, after_time, actions,
                     enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    rule_id,
                    name,
                    kind,
                    trigger_phrase,
                    after_time,
                    json.dumps(actions),
                    now,
                    now,
                ),
            )
        rule = self.get_rule(rule_id)
        if rule is None:
            raise RuntimeError("The saved rule could not be read back.")
        return rule

    def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM rules WHERE rule_id = ?", (rule_id,)
            ).fetchone()
        return self._rule(row) if row else None

    def list_rules(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM rules"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at, rule_id"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [self._rule(row) for row in rows]

    def update_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE rules
                SET name = ?, kind = ?, trigger_phrase = ?, after_time = ?,
                    actions = ?, updated_at = ?
                WHERE rule_id = ?
                """,
                (
                    rule["name"],
                    rule["kind"],
                    rule["trigger_phrase"],
                    rule["after_time"],
                    json.dumps(rule["actions"]),
                    _now(),
                    rule["rule_id"],
                ),
            )
            if result.rowcount != 1:
                raise ValueError(f"Unknown rule: {rule['rule_id']}")
        updated = self.get_rule(rule["rule_id"])
        if updated is None:
            raise RuntimeError("The updated rule could not be read back.")
        return updated

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> dict[str, Any]:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE rules SET enabled = ?, updated_at = ? WHERE rule_id = ?",
                (int(enabled), _now(), rule_id),
            )
            if result.rowcount != 1:
                raise ValueError(f"Unknown rule: {rule_id}")
        rule = self.get_rule(rule_id)
        if rule is None:
            raise RuntimeError("The rule could not be read back.")
        return rule

    def delete_rule(self, rule_id: str) -> None:
        with self._connect() as connection:
            result = connection.execute(
                "DELETE FROM rules WHERE rule_id = ?", (rule_id,)
            )
            if result.rowcount != 1:
                raise ValueError(f"Unknown rule: {rule_id}")
