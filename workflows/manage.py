from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from astrbot.api.event import AstrMessageEvent

from .formatting import format_list
from .models import WorkflowRequest
from .runtime import reply
from .utils import _first_text, _get_int


async def run_list_subscriptions(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
) -> AsyncIterator[Any]:
    try:
        limit = _get_int(request.params, "limit", default=10)
        cached_list = getattr(plugin, "cached_list_ani", None)
        if callable(cached_list):
            anis = await cached_list()
        else:
            anis = await plugin.client(require_api_key=True).list_ani()
        anis = [ani for ani in anis if bool(ani.get("enable"))]
        yield reply(event, request, format_list(anis, max(limit, 1)))
    except Exception as exc:
        yield reply(event, request, f"List subscriptions failed: {exc}")


async def run_refresh_subscription(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
) -> AsyncIterator[Any]:
    subscription_id = _first_text(
        {"target": request.target, **request.params},
        "subscription_id",
        "id",
        "target",
    )
    if not subscription_id:
        yield reply(event, request, "refresh_subscription 需要 subscription_id。")
        return

    try:
        message = await plugin.client(require_api_key=True).refresh_ani(subscription_id)
        yield reply(event, request, message)
    except Exception as exc:
        yield reply(event, request, f"Refresh failed: {exc}")


async def run_refresh_all(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
) -> AsyncIterator[Any]:
    try:
        message = await plugin.client(require_api_key=True).refresh_all()
        yield reply(event, request, message)
    except Exception as exc:
        yield reply(event, request, f"Refresh failed: {exc}")


async def run_check_status(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
) -> AsyncIterator[Any]:
    try:
        await plugin.client().ping()
        about = await plugin.client().about()
        version = ""
        if isinstance(about.get("data"), dict):
            version = str(about["data"].get("version") or "")
        suffix = f" version={version}" if version else ""
        yield reply(event, request, f"ANI-RSS connection OK.{suffix}")
    except Exception as exc:
        yield reply(event, request, f"ANI-RSS connection failed: {exc}")
