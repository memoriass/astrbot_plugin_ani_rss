from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import suppress
import re
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..integrations.ani_rss import AniRssError
from ..ui.rendering import cleanup_rendered_cards, result_for_interaction
from .formatting import (
    format_added,
    format_ani_summary,
    format_list,
    format_mikan_candidates,
    format_mikan_groups,
    format_mikan_recommendations,
    format_preview,
    format_workflow_list,
)
from .mikan import (
    _enrich_mikan_candidate,
    _enrich_mikan_candidates_for_card,
    _groups_from_candidate,
    _mikan_season_payload,
    _rank_mikan_candidates,
    mikan_candidates,
)
from .models import CANCEL_REPLIES, COMPILED_WORKFLOWS, CONFIRM_REPLIES, WorkflowRequest
from .utils import (
    _first_text,
    _get_bool,
    _get_float,
    _get_int,
    _get_optional_bool,
    _looks_like_mikan_url,
    _looks_like_rss_url,
    _normalize_workflow,
    normalize_reply,
)

DEFAULT_RECOMMEND_LIMIT = 8
DEFAULT_RECOMMEND_MIN_SCORE = 0.0
DEFAULT_RECOMMEND_INCLUDE_EXISTING = False
PREVIEW_ITEMS_LIMIT = 8

async def run_ani_rss_workflow(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
) -> AsyncIterator[Any]:
    request.workflow = _normalize_workflow(request.workflow)
    spec = COMPILED_WORKFLOWS.get(request.workflow)
    if spec is None:
        yield _reply(event, request, "未知 ANI-RSS workflow。\n" + format_workflow_list())
        return

    preflight_error = await _silent_connection_preflight(plugin, request.workflow)
    if preflight_error:
        yield _reply(
            event,
            request,
            f"ANI-RSS 服务暂时不可用，无法继续执行订阅工作流: {preflight_error}",
        )
        return

    if request.workflow == "add_subscription":
        async for item in _run_add_subscription(plugin, event, request):
            yield item
        return

    if request.workflow == "add_mikan_subscription":
        async for item in _run_add_mikan_subscription(plugin, event, request):
            yield item
        return

    if request.workflow == "recommend_mikan_subscription":
        async for item in _run_recommend_mikan_subscription(plugin, event, request):
            yield item
        return

    if request.workflow == "search_mikan":
        async for item in _run_search_mikan(plugin, event, request):
            yield item
        return

    if request.workflow == "preview_subscription":
        async for item in _run_preview_subscription(plugin, event, request):
            yield item
        return

    if request.workflow == "list_subscriptions":
        async for item in _run_list_subscriptions(plugin, event, request):
            yield item
        return

    if request.workflow == "refresh_subscription":
        async for item in _run_refresh_subscription(plugin, event, request):
            yield item
        return

    if request.workflow == "refresh_all":
        async for item in _run_refresh_all(plugin, event, request):
            yield item
        return

    if request.workflow == "check_status":
        async for item in _run_check_status(plugin, event, request):
            yield item
        return

    if request.workflow == "continue_pending":
        async for item in _run_continue_pending(plugin, event, request):
            yield item
        return

    yield _reply(event, request, "workflow 已注册但尚未实现，请检查插件版本。")

async def _silent_connection_preflight(plugin: Any, workflow: str) -> str:
    if workflow in {"check_status", "continue_pending"}:
        return ""
    try:
        await plugin.client().ping()
    except Exception as exc:
        logger.warning("ANI-RSS silent preflight failed before %s: %s", workflow, exc)
        return str(exc)
    return ""

def _reply(event: AstrMessageEvent, request: WorkflowRequest, text: str) -> Any:
    if request.source == "tool":
        return text
    return event.plain_result(text)

async def _interactive_reply(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
    text: str,
    *,
    title: str = "",
) -> Any:
    result, path = await result_for_interaction(plugin, event, text, title=title)
    if path:
        request.rendered_cards.append(path)
    return result

def _event_origin(event: AstrMessageEvent) -> str:
    return str(
        getattr(event, "unified_msg_origin", "")
        or getattr(event, "session_id", "")
        or "",
    )

def _event_message_text(event: AstrMessageEvent) -> str:
    text_getter = getattr(event, "get_message_str", None)
    if callable(text_getter):
        return str(text_getter() or "")
    return str(getattr(event, "message_str", "") or "")

def _event_text_bundle(event: AstrMessageEvent) -> str:
    parts = [_event_message_text(event)]
    message_obj = getattr(event, "message_obj", None)
    for comp in getattr(message_obj, "message", []) or []:
        message_str = getattr(comp, "message_str", "")
        if message_str:
            parts.append(str(message_str))
        for reply_comp in getattr(comp, "chain", []) or []:
            text = getattr(reply_comp, "text", "")
            if text:
                parts.append(str(text))
    return "\n".join(part for part in parts if part)

def _extract_task_id(text: str) -> str:
    match = re.search(r"\bani-[0-9a-fA-F]{8}\b", str(text or ""))
    return match.group(0).lower() if match else ""

def _extract_task_ref(text: str) -> str:
    raw = str(text or "")
    full = _extract_task_id(raw)
    if full:
        return full
    prefixed = re.search(r"\bani-([0-9a-fA-F]{3,8})\b", raw)
    if prefixed:
        return f"ani-{prefixed.group(1).lower()}"
    fragment = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{3,8})(?![0-9a-fA-F])", raw)
    return fragment.group(1).lower() if fragment else ""

def _resolve_task_ref(event: AstrMessageEvent, request: WorkflowRequest) -> str:
    task_id = _first_text(
        {"target": request.target, **request.params},
        "task_id",
        "task",
        "pending_task_id",
        "id",
        "target",
    )
    if task_id:
        extracted = _extract_task_ref(task_id)
        return extracted or task_id.strip()
    return _extract_task_ref(_event_text_bundle(event))

def _resolve_pending_task(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
) -> tuple[str, dict[str, Any] | None, str]:
    task_ref = _resolve_task_ref(event, request)
    if not task_ref:
        example = _command_example(plugin, event, "anirss 继续 <任务ID前后几位> <序号|确认|取消>")
        return "", None, f"继续 ANI-RSS 挂起任务需要 task_id。请引用带任务ID的消息，或使用 `{example}`。"

    resolver = getattr(plugin, "resolve_pending_task_id", None)
    if callable(resolver):
        task_id, matches = resolver(task_ref, origin=_event_origin(event))
        if matches:
            short = "、".join(matches[:5])
            suffix = " ..." if len(matches) > 5 else ""
            return "", None, f"任务ID片段 `{task_ref}` 匹配到多个挂起任务: {short}{suffix}。请多输入几位。"
        if not task_id:
            return "", None, f"ANI-RSS 挂起任务不存在或已过期: {task_ref}"
    else:
        task_id = task_ref

    task = plugin.get_pending_task(task_id)
    if not task:
        return "", None, f"ANI-RSS 挂起任务不存在或已过期: {task_ref}"
    if not _task_origin_matches(event, task):
        return "", None, f"ANI-RSS 挂起任务不属于当前会话: {task_id}"
    return task_id, task, ""

def _resolve_pending_action(event: AstrMessageEvent, request: WorkflowRequest) -> str:
    action = _first_text(
        {"target": request.target, **request.params},
        "choice",
        "selection",
        "index",
        "action",
        "confirm",
    )
    if action and not _extract_task_id(action):
        return action
    text = _event_message_text(event).strip()
    return text or action

def _extract_choice_index(action: str, max_index: int) -> int | None:
    normalized = normalize_reply(action)
    if not normalized:
        return None
    try:
        value = int(normalized)
    except ValueError:
        match = re.search(r"(?:选|选择|第)?\s*(\d{1,2})\s*(?:个|项|部)?", action)
        if not match:
            return None
        value = int(match.group(1))
    if not 1 <= value <= max_index:
        return None
    return value - 1

def _command_example(plugin: Any, event: AstrMessageEvent, command: str) -> str:
    command_example = getattr(plugin, "command_example", None)
    if callable(command_example):
        with suppress(Exception):
            return command_example(command, event)
    return f"/{command}"

def _pending_footer(plugin: Any, event: AstrMessageEvent, task_id: str, action: str) -> str:
    short_id = task_id.rsplit("-", maxsplit=1)[-1][-4:]
    example = _command_example(plugin, event, f"anirss 继续 {short_id} {action}")
    return (
        f"\n\n任务ID: {task_id}\n"
        f"状态: 已挂起。引用本消息回复“{action}”，"
        f"或发送 `{example}` 继续。"
        "任务ID可用当前会话内不冲突的前几位或后几位。"
    )

def _request_from_task(task: dict[str, Any], source: str) -> WorkflowRequest:
    request_data = task.get("request") if isinstance(task.get("request"), dict) else {}
    return WorkflowRequest(
        workflow=str(request_data.get("workflow") or task.get("workflow") or ""),
        target=str(request_data.get("target") or ""),
        params=dict(request_data.get("params") or {}),
        source=source,
    )

def _store_pending_task(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
    *,
    kind: str,
    payload: dict[str, Any],
    task_id: str = "",
) -> tuple[str, dict[str, Any]]:
    task = {
        "task_id": task_id,
        "kind": kind,
        "origin": _event_origin(event),
        "workflow": request.workflow,
        "request": {
            "workflow": request.workflow,
            "target": request.target,
            "params": dict(request.params),
        },
        "rendered_cards": [],
        **payload,
    }
    task_id = plugin.create_pending_task(task)
    stored = plugin.get_pending_task(task_id) or task
    return task_id, stored

def _cleanup_task_cards(task: dict[str, Any] | None) -> None:
    if task:
        cleanup_rendered_cards(task.get("rendered_cards") or [])

def _task_origin_matches(event: AstrMessageEvent, task: dict[str, Any]) -> bool:
    origin = str(task.get("origin") or "")
    return not origin or origin == _event_origin(event)

async def _run_add_subscription(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
) -> AsyncIterator[Any]:
    try:
        ani = await _build_from_request(plugin, request)
        body = "\n".join(
            [
                "待确认 ANI-RSS 订阅:",
                format_ani_summary(ani),
                "确认后仅提交到 ANI-RSS；下载路径、过滤规则和后续刷新由 ANI-RSS 处理。",
            ],
        )
    except AniRssError as exc:
        yield _reply(event, request, f"ANI-RSS 添加流程启动失败: {exc}")
        return
    except Exception as exc:
        logger.exception("Unexpected ANI-RSS add workflow preparation failure")
        yield _reply(event, request, f"ANI-RSS 添加流程启动失败: {exc}")
        return

    task_id, task = _store_pending_task(
        plugin,
        event,
        request,
        kind="confirm_add",
        payload={"ani": ani},
    )
    yield await _interactive_reply(
        plugin,
        event,
        request,
        body + _pending_footer(plugin, event, task_id, "确认"),
    )
    task["rendered_cards"] = list(request.rendered_cards)

async def _run_add_mikan_subscription(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
) -> AsyncIterator[Any]:
    params = request.params
    rss_url = _first_text({"target": request.target, **params}, "rss_url", "url")
    if _looks_like_rss_url(rss_url):
        request.target = rss_url
        request.params["rss_type"] = "mikan"
        async for item in _run_add_subscription(plugin, event, request):
            yield item
        return

    query = _first_text(
        {"target": request.target, **params},
        "query",
        "title",
        "name",
        "target",
    )
    mikan_url = _first_text(params, "mikan_url", "bangumi_url")
    if not query and not mikan_url:
        yield _reply(event, request, "add_mikan_subscription 需要番名 query，或 mikan_url/rss_url。")
        return

    async for item in _run_mikan_add_steps(plugin, event, request):
        yield item

async def _run_mikan_add_steps(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
) -> AsyncIterator[Any]:
    try:
        params = request.params
        mikan_url = _first_text(
            {"target": request.target, **params},
            "mikan_url",
            "bangumi_url",
        )
        candidate: dict[str, Any] | None = None
        if not mikan_url:
            query = _first_text(
                {"target": request.target, **params},
                "query",
                "title",
                "name",
                "target",
            )
            data = await plugin.client(require_api_key=True).mikan_search(query)
            candidates = mikan_candidates(data)
            if not candidates:
                yield _reply(event, request, f"未搜索到 Mikan 番剧：{query}")
                return

            if len(candidates) == 1:
                candidate = candidates[0]
            else:
                shown = await _enrich_mikan_candidates_for_card(
                    plugin,
                    candidates[:8],
                )
                task_id, task = _store_pending_task(
                    plugin,
                    event,
                    request,
                    kind="select_mikan_anime",
                    payload={"candidates": shown},
                )
                yield await _interactive_reply(
                    plugin,
                    event,
                    request,
                    format_mikan_candidates(
                        shown,
                        limit=8,
                        heading="Mikan 选择要订阅的番剧",
                    )
                    + _pending_footer(plugin, event, task_id, "选 1"),
                )
                task["rendered_cards"] = list(request.rendered_cards)
                return
            candidate = await _enrich_mikan_candidate(plugin, candidate)
            mikan_url = str(candidate.get("url") or "")
            if not mikan_url:
                yield _reply(event, request, "所选 Mikan 番剧没有可用链接，无法继续添加。")
                return

        async for item in _select_mikan_group(
            plugin,
            event,
            request,
            mikan_url,
            candidate=candidate,
        ):
            yield item
    except AniRssError as exc:
        yield _reply(event, request, f"Mikan 添加 workflow 失败: {exc}")
    except Exception as exc:
        logger.exception("Unexpected Mikan add workflow failure")
        yield _reply(event, request, f"Mikan 添加 workflow 失败: {exc}")

async def _run_recommend_mikan_subscription(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
) -> AsyncIterator[Any]:
    params = request.params
    query = _first_text({"target": request.target, **params}, "query", "title", "name", "target")
    limit = _get_int(
        params,
        "limit",
        default=DEFAULT_RECOMMEND_LIMIT,
    )
    min_score = _get_float(
        params,
        "min_score",
        default=DEFAULT_RECOMMEND_MIN_SCORE,
    )
    include_existing = _get_bool(
        params,
        "include_existing",
        default=DEFAULT_RECOMMEND_INCLUDE_EXISTING,
    )
    preview_only = _get_bool(
        params,
        "preview_only",
        default=False,
    ) or _get_bool(
        params,
        "dry_run",
        default=False,
    )
    if _get_bool(params, "interactive", default=False):
        preview_only = False
    payload = _mikan_season_payload(params)

    try:
        data = await plugin.client(require_api_key=True).mikan_search(query, payload)
        candidates = mikan_candidates(data)
        candidates = _rank_mikan_candidates(
            candidates,
            min_score=min_score,
            include_existing=include_existing,
        )
        if not candidates:
            yield _reply(
                event,
                request,
                "没有符合条件的 Mikan 推荐结果。"
                f"\nquery={query or '(当前季度)'} min_score={min_score:g}"
                f" include_existing={include_existing}",
            )
            return

        shown = candidates[: max(limit, 1)]
        body = format_mikan_recommendations(
            shown,
            total=len(candidates),
            query=query,
            season=payload,
        )
        if preview_only:
            yield _reply(event, request, body)
            return

        task_id, task = _store_pending_task(
            plugin,
            event,
            request,
            kind="select_mikan_anime",
            payload={"candidates": shown},
        )
        yield await _interactive_reply(
            plugin,
            event,
            request,
            body + _pending_footer(plugin, event, task_id, "选 1"),
        )
        task["rendered_cards"] = list(request.rendered_cards)
    except AniRssError as exc:
        yield _reply(event, request, f"Mikan 推荐 workflow 失败: {exc}")
    except Exception as exc:
        logger.exception("Unexpected Mikan recommend workflow failure")
        yield _reply(event, request, f"Mikan 推荐 workflow 失败: {exc}")

async def _select_mikan_group(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
    mikan_url: str,
    *,
    candidate: dict[str, Any] | None = None,
    task_id: str = "",
) -> AsyncIterator[Any]:
    if not mikan_url:
        yield _reply(event, request, "Mikan 番剧 URL 为空，无法继续。")
        return

    if not candidate or not candidate.get("bgmUrl") or not candidate.get("groups"):
        candidate = await _enrich_mikan_candidate(plugin, candidate or {"url": mikan_url})

    groups = _groups_from_candidate(candidate)
    if not groups:
        groups = await plugin.client(require_api_key=True).mikan_groups(mikan_url)
    groups = [group for group in groups if str(group.get("rss") or "").strip()]
    if not groups:
        yield _reply(event, request, "该 Mikan 番剧没有找到可用字幕组 RSS。")
        return

    shown_groups = groups[:8]
    task_id, task = _store_pending_task(
        plugin,
        event,
        request,
        kind="select_mikan_group",
        payload={
            "candidate": candidate or {},
            "groups": shown_groups,
            "mikan_url": mikan_url,
        },
        task_id=task_id,
    )
    yield await _interactive_reply(
        plugin,
        event,
        request,
        format_mikan_groups(shown_groups, limit=8)
        + _pending_footer(plugin, event, task_id, "选 1"),
    )
    task["rendered_cards"] = list(request.rendered_cards)

async def _run_continue_pending(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
) -> AsyncIterator[Any]:
    task_id, task, error = _resolve_pending_task(plugin, event, request)
    if error or not task_id or not task:
        yield _reply(event, request, error or "无法解析 ANI-RSS 挂起任务。")
        return

    action = _resolve_pending_action(event, request)
    if normalize_reply(action) in CANCEL_REPLIES:
        popped = plugin.pop_pending_task(task_id)
        _cleanup_task_cards(popped)
        yield _reply(event, request, f"已取消 ANI-RSS 挂起任务: {task_id}")
        return

    kind = str(task.get("kind") or "")
    if kind == "confirm_add":
        async for item in _continue_confirm_add(plugin, event, request, task_id, task, action):
            yield item
        return
    if kind == "select_mikan_anime":
        async for item in _continue_select_mikan_anime(plugin, event, request, task_id, task, action):
            yield item
        return
    if kind == "select_mikan_group":
        async for item in _continue_select_mikan_group(plugin, event, request, task_id, task, action):
            yield item
        return

    yield _reply(event, request, f"无法识别的 ANI-RSS 挂起任务类型: {kind or '(empty)'}")

async def _continue_confirm_add(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
    task_id: str,
    task: dict[str, Any],
    action: str,
) -> AsyncIterator[Any]:
    if normalize_reply(action) not in CONFIRM_REPLIES:
        yield _reply(event, request, f"任务 {task_id} 等待确认。请回复“确认”添加，或回复“取消”放弃。")
        return

    popped = plugin.pop_pending_task(task_id)
    _cleanup_task_cards(popped)
    ani = dict(task.get("ani") or {})
    if not ani:
        yield _reply(event, request, f"任务 {task_id} 缺少订阅内容，无法添加。")
        return
    try:
        message = await plugin.client(require_api_key=True).add_ani(ani)
        ani["_message"] = message
        yield _reply(event, request, format_added(ani))
    except Exception as exc:
        logger.exception("Unexpected ANI-RSS pending add failure")
        yield _reply(event, request, f"添加订阅失败: {exc}")

async def _continue_select_mikan_anime(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
    task_id: str,
    task: dict[str, Any],
    action: str,
) -> AsyncIterator[Any]:
    candidates = [item for item in task.get("candidates") or [] if isinstance(item, dict)]
    if not candidates:
        yield _reply(event, request, f"任务 {task_id} 没有可用番剧候选。")
        return
    selected = _extract_choice_index(action, len(candidates))
    if selected is None:
        yield _reply(event, request, f"任务 {task_id} 等待选择番剧。请回复 1-{len(candidates)}，或回复“取消”。")
        return

    popped = plugin.pop_pending_task(task_id)
    _cleanup_task_cards(popped)
    resumed_request = _request_from_task(task, request.source)
    try:
        candidate = await _enrich_mikan_candidate(plugin, candidates[selected])
        mikan_url = str(candidate.get("url") or "")
        async for item in _select_mikan_group(
            plugin,
            event,
            resumed_request,
            mikan_url,
            candidate=candidate,
            task_id=task_id,
        ):
            yield item
    except Exception as exc:
        logger.exception("Unexpected ANI-RSS pending Mikan anime selection failure")
        yield _reply(event, request, f"继续 Mikan 番剧选择失败: {exc}")

async def _continue_select_mikan_group(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
    task_id: str,
    task: dict[str, Any],
    action: str,
) -> AsyncIterator[Any]:
    groups = [item for item in task.get("groups") or [] if isinstance(item, dict)]
    if not groups:
        yield _reply(event, request, f"任务 {task_id} 没有可用字幕组候选。")
        return
    selected = _extract_choice_index(action, len(groups))
    if selected is None:
        yield _reply(event, request, f"任务 {task_id} 等待选择字幕组。请回复 1-{len(groups)}，或回复“取消”。")
        return

    popped = plugin.pop_pending_task(task_id)
    _cleanup_task_cards(popped)
    group = groups[selected]
    rss_url = str(group.get("rss") or "")
    if not rss_url:
        yield _reply(event, request, "所选字幕组没有 RSS 地址，无法添加。")
        return

    resumed_request = _request_from_task(task, request.source)
    candidate = task.get("candidate") if isinstance(task.get("candidate"), dict) else {}
    resumed_request.target = rss_url
    resumed_request.params["rss_type"] = "mikan"
    resumed_request.params.setdefault("subgroup", str(group.get("label") or ""))
    bgm_url = str(group.get("bgmUrl") or candidate.get("bgmUrl") or "")
    if bgm_url:
        resumed_request.params.setdefault("bgm_url", bgm_url)
    try:
        ani = await _build_from_request(plugin, resumed_request)
        message = await plugin.client(require_api_key=True).add_ani(ani)
        ani["_message"] = message
        yield _reply(event, request, format_added(ani))
    except Exception as exc:
        logger.exception("Unexpected Mikan pending auto add failure")
        yield _reply(event, request, f"添加订阅失败: {exc}")

async def _run_search_mikan(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
) -> AsyncIterator[Any]:
    params = request.params
    mikan_url = _first_text(
        {"target": request.target, **params},
        "mikan_url",
        "bangumi_url",
        "url",
        "target",
    )
    try:
        if _looks_like_mikan_url(mikan_url):
            groups = await plugin.client(require_api_key=True).mikan_groups(mikan_url)
            if not groups:
                yield _reply(event, request, "未找到 Mikan 字幕组。")
                return

            shown_groups = groups[:8]
            task_id, task = _store_pending_task(
                plugin,
                event,
                request,
                kind="select_mikan_group",
                payload={
                    "candidate": {},
                    "groups": shown_groups,
                    "mikan_url": mikan_url,
                },
            )
            yield await _interactive_reply(
                plugin,
                event,
                request,
                format_mikan_groups(shown_groups, limit=8)
                + _pending_footer(plugin, event, task_id, "选 1"),
            )
            task["rendered_cards"] = list(request.rendered_cards)
            return

        query = _first_text({"target": request.target, **params}, "query", "title", "target")
        if not query:
            yield _reply(event, request, "search_mikan 需要 query 或 mikan_url。")
            return
        data = await plugin.client(require_api_key=True).mikan_search(query)
        candidates = mikan_candidates(data)
        if not candidates:
            yield _reply(event, request, "未搜索到 Mikan 番剧。")
            return

        shown = await _enrich_mikan_candidates_for_card(plugin, candidates[:10])
        task_id, task = _store_pending_task(
            plugin,
            event,
            request,
            kind="select_mikan_anime",
            payload={"candidates": shown},
        )
        yield await _interactive_reply(
            plugin,
            event,
            request,
            format_mikan_candidates(shown, limit=10)
            + _pending_footer(plugin, event, task_id, "选 1"),
        )
        task["rendered_cards"] = list(request.rendered_cards)
    except Exception as exc:
        yield _reply(event, request, f"Mikan search failed: {exc}")

async def _run_preview_subscription(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
) -> AsyncIterator[Any]:
    try:
        ani = await _build_from_request(plugin, request)
        preview = await plugin.client(require_api_key=True).preview_ani(ani)
        yield _reply(event, request, format_preview(ani, preview, PREVIEW_ITEMS_LIMIT))
    except Exception as exc:
        yield _reply(event, request, f"Preview failed: {exc}")

async def _run_list_subscriptions(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
) -> AsyncIterator[Any]:
    try:
        limit = _get_int(request.params, "limit", default=10)
        enabled_only = _get_bool(request.params, "enabled_only", default=False)
        anis = await plugin.client(require_api_key=True).list_ani()
        if enabled_only:
            anis = [ani for ani in anis if bool(ani.get("enable"))]
        yield _reply(event, request, format_list(anis, max(limit, 1)))
    except Exception as exc:
        yield _reply(event, request, f"List subscriptions failed: {exc}")

async def _run_refresh_subscription(
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
        yield _reply(event, request, "refresh_subscription 需要 subscription_id。")
        return

    try:
        message = await plugin.client(require_api_key=True).refresh_ani(subscription_id)
        yield _reply(event, request, message)
    except Exception as exc:
        yield _reply(event, request, f"Refresh failed: {exc}")

async def _run_refresh_all(
    plugin: Any,
    event: AstrMessageEvent,
    request: WorkflowRequest,
) -> AsyncIterator[Any]:
    try:
        message = await plugin.client(require_api_key=True).refresh_all()
        yield _reply(event, request, message)
    except Exception as exc:
        yield _reply(event, request, f"Refresh failed: {exc}")

async def _run_check_status(
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
        yield _reply(event, request, f"ANI-RSS connection OK.{suffix}")
    except Exception as exc:
        yield _reply(event, request, f"ANI-RSS connection failed: {exc}")

async def _build_from_request(plugin: Any, request: WorkflowRequest) -> dict[str, Any]:
    params = request.params
    rss_url = _first_text({"target": request.target, **params}, "rss_url", "url", "target")
    if not rss_url:
        raise AniRssError("rss_url is required.")
    return await plugin.build_subscription(
        rss_url=rss_url,
        rss_type=_first_text(params, "rss_type", "type") or "auto",
        bgm_url=_first_text(params, "bgm_url", "bgmUrl", "bangumi_url"),
        subgroup=_first_text(params, "subgroup"),
        enable=_get_optional_bool(params, "enable"),
    )
