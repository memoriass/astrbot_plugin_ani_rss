from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import RLock
from typing import Any

from astrbot.api import logger

from .storage_paths import PLUGIN_NAME, plugin_data_dir


class RuntimeSqliteStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = RLock()
        self.initialize()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            with suppress(sqlite3.DatabaseError):
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pending_tasks (
                    task_id TEXT PRIMARY KEY,
                    origin TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT '',
                    workflow TEXT NOT NULL DEFAULT '',
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pending_tasks_expires_at
                    ON pending_tasks(expires_at);
                CREATE INDEX IF NOT EXISTS idx_pending_tasks_origin
                    ON pending_tasks(origin);

                CREATE TABLE IF NOT EXISTS cache_entries (
                    cache_key TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cache_entries_expires_at
                    ON cache_entries(expires_at);
                """,
            )

    @contextmanager
    def _connection(self):
        with self._lock:
            conn = sqlite3.connect(str(self.db_path), timeout=3)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=3000")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def cleanup(self, now: float | None = None) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM pending_tasks WHERE expires_at <= ?",
                (current,),
            ).fetchall()
            expired = [task for row in rows if (task := _loads_dict(row["payload_json"]))]
            conn.execute("DELETE FROM pending_tasks WHERE expires_at <= ?", (current,))
            conn.execute("DELETE FROM cache_entries WHERE expires_at <= ?", (current,))
        return expired

    def pending_exists(self, task_id: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM pending_tasks WHERE task_id = ? AND expires_at > ?",
                (task_id, time.time()),
            ).fetchone()
        return row is not None

    def set_pending(self, task_id: str, task: dict[str, Any]) -> None:
        created_at = float(task.get("created_at") or time.time())
        expires_at = float(task.get("expires_at") or created_at)
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pending_tasks
                    (task_id, origin, kind, workflow, expires_at, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    str(task.get("origin") or ""),
                    str(task.get("kind") or ""),
                    str(task.get("workflow") or ""),
                    expires_at,
                    created_at,
                    _dumps(task),
                ),
            )

    def get_pending(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT payload_json, expires_at FROM pending_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            if float(row["expires_at"] or 0) <= time.time():
                conn.execute("DELETE FROM pending_tasks WHERE task_id = ?", (task_id,))
                return None
        return _loads_dict(row["payload_json"])

    def pop_pending(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT payload_json, expires_at FROM pending_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM pending_tasks WHERE task_id = ?", (task_id,))
            if float(row["expires_at"] or 0) <= time.time():
                return None
        return _loads_dict(row["payload_json"])

    def update_pending(
        self,
        task_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT payload_json, expires_at, created_at FROM pending_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            if float(row["expires_at"] or 0) <= time.time():
                conn.execute("DELETE FROM pending_tasks WHERE task_id = ?", (task_id,))
                return None
            task = _loads_dict(row["payload_json"])
            if task is None:
                conn.execute("DELETE FROM pending_tasks WHERE task_id = ?", (task_id,))
                return None
            task.update(updates)
            task["task_id"] = task_id
            task.setdefault("created_at", float(row["created_at"] or time.time()))
            task.setdefault("expires_at", float(row["expires_at"] or time.time()))
            conn.execute(
                """
                UPDATE pending_tasks
                SET origin = ?, kind = ?, workflow = ?, expires_at = ?, created_at = ?,
                    payload_json = ?
                WHERE task_id = ?
                """,
                (
                    str(task.get("origin") or ""),
                    str(task.get("kind") or ""),
                    str(task.get("workflow") or ""),
                    float(task.get("expires_at") or row["expires_at"] or time.time()),
                    float(task.get("created_at") or row["created_at"] or time.time()),
                    _dumps(task),
                    task_id,
                ),
            )
        return task

    def list_pending(self, *, origin: str = "") -> list[tuple[str, dict[str, Any]]]:
        current = time.time()
        where = "expires_at > ?"
        params: list[Any] = [current]
        if origin:
            where += " AND (origin = '' OR origin = ?)"
            params.append(origin)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT task_id, payload_json FROM pending_tasks WHERE {where}",
                params,
            ).fetchall()
        tasks: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            task = _loads_dict(row["payload_json"])
            if task is not None:
                tasks.append((str(row["task_id"]), task))
        return tasks

    def get_cache(self, cache_key: str) -> Any | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT payload_json, expires_at FROM cache_entries WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            if float(row["expires_at"] or 0) <= time.time():
                conn.execute("DELETE FROM cache_entries WHERE cache_key = ?", (cache_key,))
                return None
        return _loads(row["payload_json"])

    def set_cache(self, cache_key: str, value: Any, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        created_at = time.time()
        expires_at = created_at + ttl_seconds
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO cache_entries
                    (cache_key, expires_at, created_at, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (cache_key, expires_at, created_at, _dumps(value)),
            )

    def delete_cache(self, cache_key: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM cache_entries WHERE cache_key = ?", (cache_key,))

    def stats(self) -> dict[str, Any]:
        current = time.time()
        with self._connection() as conn:
            pending_count = conn.execute(
                "SELECT COUNT(*) AS value FROM pending_tasks WHERE expires_at > ?",
                (current,),
            ).fetchone()["value"]
            cache_count = conn.execute(
                "SELECT COUNT(*) AS value FROM cache_entries WHERE expires_at > ?",
                (current,),
            ).fetchone()["value"]
            cache_rows = conn.execute(
                """
                SELECT cache_key, expires_at
                FROM cache_entries
                WHERE expires_at > ?
                ORDER BY expires_at ASC
                LIMIT 8
                """,
                (current,),
            ).fetchall()
        return {
            "db_path": str(self.db_path),
            "pending_count": int(pending_count or 0),
            "cache_count": int(cache_count or 0),
            "cache_keys": [
                {
                    "key": str(row["cache_key"]),
                    "ttl_seconds": max(int(float(row["expires_at"]) - current), 0),
                }
                for row in cache_rows
            ],
        }


class RuntimeStorageMixin:
    def runtime_store(self) -> RuntimeSqliteStore:
        store = getattr(self, "_ani_rss_runtime_store", None)
        if isinstance(store, RuntimeSqliteStore):
            return store
        plugin_name = str(getattr(self, "name", "") or PLUGIN_NAME)
        store = RuntimeSqliteStore(plugin_data_dir(plugin_name) / "state.sqlite3")
        self._ani_rss_runtime_store = store
        return store

    def runtime_db_path(self) -> Path:
        return self.runtime_store().db_path

    def runtime_stats(self) -> dict[str, Any]:
        return self.runtime_store().stats()

    def cleanup_runtime_store(self) -> None:
        expired = self.runtime_store().cleanup()
        for task in expired:
            with suppress(Exception):
                from ..ui.rendering import cleanup_rendered_cards

                cleanup_rendered_cards(task.get("rendered_cards") or [])

    def start_runtime_cleanup(self) -> None:
        task = getattr(self, "_ani_rss_cleanup_task", None)
        if task and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._ani_rss_cleanup_task = loop.create_task(self._runtime_cleanup_loop())

    async def stop_runtime_cleanup(self) -> None:
        task = getattr(self, "_ani_rss_cleanup_task", None)
        if not task:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._ani_rss_cleanup_task = None

    async def _runtime_cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.storage_cleanup_interval_seconds())
            try:
                self.cleanup_runtime_store()
            except Exception:
                logger.exception("ANI-RSS runtime store cleanup failed")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(raw: str) -> Any | None:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def _loads_dict(raw: str) -> dict[str, Any] | None:
    value = _loads(raw)
    return value if isinstance(value, dict) else None
