from __future__ import annotations

import json
import re
import secrets
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .integrations.ani_rss import AniRssClient, AniRssError
from .workflows import (
    format_workflow_list,
    run_ani_rss_workflow,
    workflow_from_cli,
    workflow_from_tool,
)


def _normalize_task_ref(value: str) -> str:
    text = str(value or "").strip().lower()
    full = re.search(r"\bani-[0-9a-f]{8}\b", text)
    if full:
        return full.group(0)
    prefixed = re.search(r"\bani-([0-9a-f]{3,8})\b", text)
    if prefixed:
        return f"ani-{prefixed.group(1)}"
    fragment = re.search(r"(?<![0-9a-f])([0-9a-f]{3,8})(?![0-9a-f])", text)
    return fragment.group(1) if fragment else ""


def _normalize_wake_prefixes(value: Any) -> list[str]:
    if isinstance(value, str):
        prefixes = [value]
    elif isinstance(value, list | tuple | set):
        prefixes = [str(item) for item in value]
    else:
        prefixes = []
    cleaned = [prefix.strip() for prefix in prefixes if str(prefix).strip()]
    return cleaned or ["/"]


def _format_command(prefix: str, command: str) -> str:
    prefix = str(prefix or "").strip()
    command = str(command or "").strip()
    if not prefix:
        return command
    if re.search(r"[\w\u4e00-\u9fff]$", prefix):
        return f"{prefix} {command}"
    return f"{prefix}{command}"


@register(
    "astrbot_plugin_ani_rss",
    "memoriass",
    "ANI-RSS 订阅管理插件，支持 Agent 通过 workflow 搜索并交互式添加订阅。",
    "1.0.0",
    "https://github.com/memoriass/astrbot_plugin_ani_rss",
)
class AniRssPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._pending_tasks: dict[str, dict[str, Any]] = {}

    def _cfg(self, key: str, default: Any) -> Any:
        value = self.config.get(key, default)
        return default if value is None else value

    def client(self, *, require_api_key: bool = False) -> AniRssClient:
        api_key = str(self._cfg("api_key", "") or "").strip()
        if require_api_key and not api_key:
            raise AniRssError("ANI-RSS api_key is not configured.")
        return AniRssClient(
            base_url=str(self._cfg("base_url", "http://127.0.0.1:7789")),
            api_prefix=str(self._cfg("api_prefix", "/api")),
            api_key=api_key,
            timeout_seconds=int(self._cfg("timeout_seconds", 30)),
        )

    def workflow_timeout_seconds(self) -> int:
        try:
            value = self._cfg(
                "interaction_timeout_seconds",
                self._cfg("workflow_confirm_timeout_seconds", 120),
            )
            return max(int(value), 10)
        except (TypeError, ValueError):
            return 120

    def pending_task_ttl_seconds(self) -> int:
        try:
            return max(int(self._cfg("pending_task_ttl_seconds", 86400)), 60)
        except (TypeError, ValueError):
            return 86400

    def command_prefixes(self, event: Any | None = None) -> list[str]:
        umo = str(getattr(event, "unified_msg_origin", "") or "") if event else ""
        config: Any = None
        get_config = getattr(self.context, "get_config", None)
        if callable(get_config):
            with suppress(Exception):
                config = get_config(umo or None)
        if config is not None and hasattr(config, "get"):
            with suppress(Exception):
                return _normalize_wake_prefixes(config.get("wake_prefix", ["/"]))
        with suppress(Exception):
            from astrbot.core.config.astrbot_config import ASTRBOT_CONFIG_PATH

            path = Path(ASTRBOT_CONFIG_PATH)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return _normalize_wake_prefixes(data.get("wake_prefix", ["/"]))
        return ["/"]

    def command_example(self, command: str, event: Any | None = None) -> str:
        prefix = self.command_prefixes(event)[0]
        return _format_command(prefix, command)

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
                    from .ui.rendering import cleanup_rendered_cards

                    cleanup_rendered_cards(task.get("rendered_cards") or [])

    def resolve_pending_task_id(self, task_ref: str, *, origin: str = "") -> tuple[str, list[str]]:
        self._cleanup_pending_tasks()
        ref = _normalize_task_ref(task_ref)
        if not ref:
            return "", []
        if re.fullmatch(r"ani-[0-9a-f]{8}", ref):
            return ref, []

        matches: list[str] = []
        for task_id, task in self._pending_tasks.items():
            if origin and str(task.get("origin") or "") not in {"", origin}:
                continue
            body = task_id.removeprefix("ani-")
            if task_id.startswith(ref) or task_id.endswith(ref) or body.startswith(ref) or body.endswith(ref):
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
                task_id = f"ani-{secrets.token_hex(4)}"
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

    def render_mode(self) -> str:
        mode = str(self._cfg("render_mode", "image") or "image").strip().lower()
        if mode not in {"text", "image", "both"}:
            return "image"
        return mode

    def render_width(self) -> int:
        return 860

    def render_scale(self) -> int:
        return 2

    def render_output_dir(self) -> str:
        return ""

    def cover_proxy_config(self) -> dict[str, Any]:
        api_prefix = str(self._cfg("api_prefix", "/api") or "/api")
        if api_prefix and not api_prefix.startswith("/"):
            api_prefix = f"/{api_prefix}"
        try:
            timeout_seconds = int(self._cfg("timeout_seconds", 30))
        except (TypeError, ValueError):
            timeout_seconds = 30
        return {
            "base_url": str(self._cfg("base_url", "http://127.0.0.1:7789") or "").rstrip("/"),
            "api_prefix": api_prefix.rstrip("/"),
            "api_key": str(self._cfg("api_key", "") or "").strip(),
            "timeout_seconds": timeout_seconds,
        }

    def _resolve_type(self, rss_url: str, rss_type: str = "") -> str:
        rss_type = (rss_type or "").strip()
        if rss_type and rss_type != "auto":
            return rss_type

        host = urlparse(rss_url).netloc.lower()
        if "mikan" in host:
            return "mikan"
        return "other"

    async def _resolve_mikan_bgm_url(self, rss_url: str) -> str:
        bangumi_id = ""
        parsed = urlparse(rss_url)
        query = parse_qs(parsed.query)
        if query.get("bangumiId"):
            bangumi_id = str(query["bangumiId"][0]).strip()
        if not bangumi_id:
            match = re.search(r"/Bangumi/(\d+)", rss_url)
            bangumi_id = match.group(1) if match else ""
        if not bangumi_id:
            return ""
        try:
            data = await self.client(require_api_key=True).mikan_search(f"bangumiId: {bangumi_id}")
        except Exception:
            return ""
        for section in data.get("weeks") or data.get("items") or []:
            if not isinstance(section, dict):
                continue
            for item in section.get("items") or []:
                if isinstance(item, dict) and item.get("bgmUrl"):
                    return str(item["bgmUrl"]).strip()
        return ""

    async def build_subscription(
        self,
        *,
        rss_url: str,
        rss_type: str = "",
        bgm_url: str = "",
        subgroup: str = "",
        enable: bool | None = None,
    ) -> dict[str, Any]:
        rss_url = rss_url.strip()
        if not rss_url:
            raise AniRssError("rss_url is required.")

        resolved_type = self._resolve_type(rss_url, rss_type)
        if resolved_type == "mikan" and not bgm_url.strip():
            bgm_url = await self._resolve_mikan_bgm_url(rss_url)
        if resolved_type == "other" and not bgm_url.strip():
            raise AniRssError("bgm_url is required when rss_type is other.")

        actual_enable = True if enable is None else bool(enable)
        ani = await self.client(require_api_key=True).rss_to_ani(
            rss_url=rss_url,
            rss_type=resolved_type,
            bgm_url=bgm_url,
            subgroup=subgroup,
            enable=actual_enable,
        )
        if subgroup.strip():
            ani["subgroup"] = subgroup.strip()
        if bgm_url.strip() and not ani.get("bgmUrl"):
            ani["bgmUrl"] = bgm_url.strip()
        return ani

    @filter.llm_tool(name="ani_rss")
    async def ani_rss(
        self,
        event: Any,
        workflow: str,
        target: str = "",
        params: object = "",
    ):
        """ANI-RSS 订阅助手的自然语言工作流工具。

        当用户提到 ANI-RSS、ani-rss、anirss、Mikan、蜜柑、番剧订阅、追番、
        RSS 订阅、字幕组、搜索番剧、推荐番剧或添加订阅时，优先使用本工具。
        不要要求用户先检查连接；插件会在真实业务 workflow 开始前静默检测
        ANI-RSS API 连通性，只有检测失败才向用户说明服务不可用。

        模型需要从用户自然语言中选择一个 workflow，并补齐 target/params。不要让用户
        输入命令或 workflow ID。插件会在内部完成 RSS 转换、卡片交互、
        用户选择、挂起任务恢复和 ANI-RSS API 调用。

        自然语言到 workflow 的映射：
            "列出当季推荐番剧", "有什么本季新番推荐", "推荐几部新番"
                -> workflow=recommend_mikan_subscription，target 留空，params 可包含
                   {"limit":8,"min_score":7,"year":2026,"season":"春"}。
                   如果用户说“只展示候选 / 先看看 / 不要添加订阅”，必须同时设置
                   {"preview_only": true}，不要进入选择或添加流程。
                   普通推荐会返回候选并挂起一个任务；用户引用候选消息回复“选 1”时，
                   从引用文本中提取任务ID，调用 workflow=continue_pending，并传入
                   {"task_id":"ani-xxxxxxxx","choice":"1"}。
                   task_id 也可以只传当前会话内不冲突的前几位或后几位，
                   例如 {"task_id":"3c4d","choice":"1"}。
            "帮我添加/下载/订阅 淡岛百景 到 ANI/NAS", "帮我追番 xxx"
                -> workflow=add_mikan_subscription，target/query 是番名。
                   ANI/NAS 是目标服务提示，不是下载路径；不要设置路径参数。
                   下载目录、过滤规则和后续下载由 ANI-RSS 处理。
            "查一下 xxx 有没有字幕组", "搜索 Mikan xxx"
                -> workflow=search_mikan，target/query 是番名。
            "查看订阅/列出订阅列表"
                -> workflow=list_subscriptions.
            "刷新全部订阅/更新 ANI-RSS"
                -> workflow=refresh_all；只有用户给出明确订阅 id 时才使用
                   refresh_subscription。
            用户引用 ANI-RSS 候选/选择消息并回复“选 1”、“确认”或“取消”
                -> workflow=continue_pending，params 包含 task_id 和 choice/action。

        Interactive result handling:
            Candidate and selection cards are non-blocking. The plugin returns
            a task id such as ani-1a2b3c4d and stores the pending workflow state.
            When the user quotes that card and replies with a number/confirmation,
            call continue_pending with the quoted task id. A short unique prefix
            or suffix of the task id is also accepted. After a Mikan subgroup is
            selected, the subscription is added automatically.

        Supported workflow scenarios:
            add_subscription - build a subscription from RSS, ask for a short
                confirmation, then write it to ANI-RSS.
            add_mikan_subscription - search Mikan by title, ask the user to
                choose anime and subgroup, then add the subscription.
            recommend_mikan_subscription - discover seasonal or keyword Mikan
                anime, rank by ANI-RSS/Bangumi score, then reuse the same
                subgroup selection and add flow.
            search_mikan - search Mikan anime or list subgroup RSS for a Mikan URL.
            preview_subscription - diagnostic-only preview, not used in normal
                chat flows.
            list_subscriptions - list current subscriptions.
            refresh_subscription - refresh one subscription by id.
            refresh_all - refresh all subscriptions.
            check_status - diagnostic-only connectivity check. Normal chat
                should use search/recommend/add/list/refresh workflows; their
                connectivity check is performed silently before the business step.
            continue_pending - resume a suspended ANI-RSS task by task_id and a
                choice/action extracted from the user's quoted reply.

        Args:
            workflow(string): One workflow scenario id from the list above.
            target(string): RSS URL for add/diagnostic preview, or subscription id for refresh.
            params(object): Optional JSON object. Common fields:
                add_subscription:
                    {"rss_url":"https://...","rss_type":"auto","bgm_url":"",
                     "subgroup":"","enable":true}
                add_mikan_subscription:
                    {"query":"番名"} or {"mikan_url":"https://mikanime.tv/Home/Bangumi/3878"}
                recommend_mikan_subscription:
                    {"query":"","year":2026,"season":"春","min_score":7,
                     "limit":8,"include_existing":false,"preview_only":true}
                search_mikan:
                    {"query":"番名"} or {"mikan_url":"https://mikanime.tv/Home/Bangumi/3878"}
                list_subscriptions:
                    {"limit":10,"enabled_only":false}
                refresh_subscription:
                    {"subscription_id":"..."}
                continue_pending:
                    {"task_id":"ani-1a2b3c4d","choice":"1"} or
                    {"task_id":"3c4d","action":"确认"}
        """
        request = workflow_from_tool(workflow, target, params)
        actual_event = self._message_event_from_tool_arg(event)
        self._apply_tool_prompt_flags(actual_event, request)
        async for item in run_ani_rss_workflow(self, actual_event, request):
            yield item

    @staticmethod
    def _message_event_from_tool_arg(event: Any) -> AstrMessageEvent:
        context = getattr(event, "context", None)
        actual_event = getattr(context, "event", None)
        return actual_event or event

    @staticmethod
    def _apply_tool_prompt_flags(event: Any, request: Any) -> None:
        if request.source != "tool":
            return
        text_getter = getattr(event, "get_message_str", None)
        text = text_getter() if callable(text_getter) else getattr(event, "message_str", "")
        if not isinstance(text, str):
            text = str(text or "")
        no_add = re.search(r"(?:只展示|先只|先看看|先看|看看|不要|不用|先不|无需|别).{0,20}(?:添加|下载|订阅|追番|加入|候选)", text)
        if no_add and request.workflow == "recommend_mikan_subscription":
            request.params.setdefault("preview_only", True)
        if no_add and request.workflow == "add_mikan_subscription":
            request.workflow = "search_mikan"

    def _help_text(self, event: AstrMessageEvent) -> str:
        def cmd(command: str) -> str:
            return self.command_example(command, event)

        return (
            "ANI-RSS 订阅助手：\n"
            f"{cmd('anirss add_subscription <rss_url>')}       - 确认后添加 RSS 订阅\n"
            f"{cmd('anirss add_mikan_subscription <番名>')}    - 搜索 Mikan，选择番剧/字幕组后添加\n"
            f"{cmd('anirss recommend_mikan_subscription [query|year season]')} - 推荐筛选后添加\n"
            f"{cmd('anirss search_mikan <番名或Mikan链接>')}   - 搜索 Mikan 或查看字幕组 RSS\n"
            f"{cmd('anirss list_subscriptions [limit]')}       - 查看订阅列表\n"
            f"{cmd('anirss refresh_subscription <id>')}        - 刷新单个订阅\n"
            f"{cmd('anirss refresh_all')}                      - 刷新全部订阅\n"
            f"{cmd('anirss 继续 <任务ID前后几位> <序号|确认|取消>')} - 继续挂起的 ANI-RSS 任务\n"
            f"{cmd('anirss check_status')}                     - 检查 ANI-RSS 连接"
        )

    @filter.command("anirss", alias={"ani-rss", "ani", "追番", "番剧订阅"})
    async def cmd_anirss(
        self,
        event: AstrMessageEvent,
        workflow: str = "help",
        args: GreedyStr = "",
    ):
        if workflow in ("help", "h", "?"):
            yield event.plain_result(self._help_text(event) + "\n\n" + format_workflow_list())
            return

        request = workflow_from_cli(workflow, args)
        if request is None:
            yield event.plain_result(
                f"不支持的 ANI-RSS workflow：{workflow}。\n" + format_workflow_list(),
            )
            return

        async for item in run_ani_rss_workflow(self, event, request):
            yield item
