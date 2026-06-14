from __future__ import annotations

import re
import secrets
import time
from contextlib import suppress
from typing import Any

from ..routing.commands import normalize_task_ref


class PendingTaskStoreMixin:
    def _cleanup_pending_tasks(self) -> None:
        now = time.time()
        expired = [
            task_id
            for task_id, task in self._pending_tasks.items()
            if float(task.get("expires_at") or 0) <= now
        ]
        for task_id in expired:
            task = self._pending_tasks.pop(task_id, None)
            if task:
                with suppress(Exception):
                    from ..ui.rendering import cleanup_rendered_cards

                    cleanup_rendered_cards(task.get("rendered_cards") or [])

    def resolve_pending_task_id(self, task_ref: str, *, origin: str = "") -> tuple[str, list[str]]:
        self._cleanup_pending_tasks()
        ref = normalize_task_ref(task_ref)
        if not ref:
            return "", []
        if re.fullmatch(r"ani[0-9a-f]{8}", ref) and ref in self._pending_tasks:
            return ref, []

        matches: list[str] = []
        body_ref = ref.removeprefix("ani")
        for task_id, task in self._pending_tasks.items():
            if origin and str(task.get("origin") or "") not in {"", origin}:
                continue
            body = task_id.removeprefix("ani")
            if (
                task_id.startswith(ref)
                or task_id.endswith(ref)
                or body.startswith(ref)
                or body.endswith(ref)
                or (body_ref and (body.startswith(body_ref) or body.endswith(body_ref)))
            ):
                matches.append(task_id)
        if len(matches) == 1:
            return matches[0], []
        if len(matches) > 1:
            return "", sorted(matches)
        return "", []

    def create_pending_task(self, task: dict[str, Any]) -> str:
        self._cleanup_pending_tasks()
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            while True:
                task_id = f"ani{secrets.token_hex(4)}"
                if task_id not in self._pending_tasks:
                    break
        now = time.time()
        stored = dict(task)
        stored["task_id"] = task_id
        stored.setdefault("created_at", now)
        stored["expires_at"] = now + self.pending_task_ttl_seconds()
        self._pending_tasks[task_id] = stored
        return task_id

    def get_pending_task(self, task_id: str) -> dict[str, Any] | None:
        self._cleanup_pending_tasks()
        return self._pending_tasks.get(str(task_id or "").strip())

    def pop_pending_task(self, task_id: str) -> dict[str, Any] | None:
        self._cleanup_pending_tasks()
        return self._pending_tasks.pop(str(task_id or "").strip(), None)
