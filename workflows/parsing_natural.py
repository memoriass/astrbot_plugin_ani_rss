from __future__ import annotations

import re
from contextlib import suppress
from typing import Any

from .models import WorkflowRequest
from .utils import (
    _looks_like_mikan_url,
    _looks_like_rss_url,
    _parse_kv_words,
)


def workflow_from_natural_language(text: str) -> WorkflowRequest | None:
    """Build a workflow request from common Chinese daily-use phrases."""
    cleaned = _clean_natural_text(text)
    if not cleaned or cleaned.startswith(("/", "\\")):
        return None

    params = _natural_common_params(cleaned)

    if _natural_status_intent(cleaned):
        return WorkflowRequest("check_status", source="natural")

    if _natural_refresh_intent(cleaned):
        subscription_id = _natural_subscription_id(cleaned)
        if subscription_id:
            return WorkflowRequest(
                "refresh_subscription",
                target=subscription_id,
                params={"subscription_id": subscription_id},
                source="natural",
            )
        return WorkflowRequest("refresh_all", source="natural")

    if _natural_recommend_intent(cleaned):
        season = _natural_season_params(cleaned)
        params.update(season)
        query = _extract_recommend_query(cleaned)
        if query:
            params["query"] = query
        return WorkflowRequest(
            "recommend_mikan_subscription",
            target=query,
            params=params,
            source="natural",
        )

    if _natural_add_intent(cleaned):
        rss_url = _extract_url(cleaned)
        if _looks_like_rss_url(rss_url):
            params["rss_url"] = rss_url
            return WorkflowRequest(
                "add_subscription",
                target=rss_url,
                params=params,
                source="natural",
            )

        title, _ = _extract_add_query_and_destination(cleaned)
        if title:
            return WorkflowRequest(
                "add_mikan_subscription",
                target=title,
                params={**params, "query": title},
                source="natural",
            )

    if _natural_search_intent(cleaned):
        mikan_url = _extract_url(cleaned)
        if _looks_like_mikan_url(mikan_url):
            return WorkflowRequest(
                "search_mikan",
                target=mikan_url,
                params={"mikan_url": mikan_url},
                source="natural",
            )
        query = _extract_search_query(cleaned)
        if query:
            return WorkflowRequest(
                "search_mikan",
                target=query,
                params={"query": query},
                source="natural",
            )

    if _natural_list_intent(cleaned):
        if re.search(r"启用|enable|enabled", cleaned, re.IGNORECASE):
            params["enabled_only"] = True
        return WorkflowRequest("list_subscriptions", params=params, source="natural")

    return None


def _clean_natural_text(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = cleaned.replace("（", "(").replace("）", ")")
    cleaned = re.sub(r"\((?:添加|下载|订阅|追番)\)", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" \t\r\n，。！？!?,;；")


def _natural_common_params(text: str) -> dict[str, Any]:
    params = _parse_kv_words(text)
    limit = _natural_limit(text)
    if limit:
        params["limit"] = limit
    min_score = _natural_min_score(text)
    if min_score is not None:
        params["min_score"] = min_score
    if re.search(r"包括已订阅|包含已订阅|include_existing\s*=\s*true", text, re.IGNORECASE):
        params["include_existing"] = True
    return params


def _natural_status_intent(text: str) -> bool:
    return bool(
        re.search(r"(?:检查|测试).{0,30}(?:ani-rss|anirss|连接|状态)", text, re.IGNORECASE),
    )


def _natural_refresh_intent(text: str) -> bool:
    return bool(
        re.search(r"(?:刷新|更新).{0,40}(?:ani-rss|anirss|全部订阅|所有订阅|订阅)", text, re.IGNORECASE),
    )


def _natural_list_intent(text: str) -> bool:
    return bool(
        re.search(r"(?:查看|列出|看看|显示).{0,40}(?:ani-rss|anirss|订阅列表|订阅)", text, re.IGNORECASE),
    )


def _natural_recommend_intent(text: str) -> bool:
    if _natural_add_intent(text) or _natural_search_intent(text):
        return False
    return bool(
        re.search(
            r"(?:列出|推荐|看看|看下|来点|有什么|有没有).{0,50}(?:当季|本季|当前季度|新番|番剧|动画|追番|推荐)",
            text,
            re.IGNORECASE,
        ),
    )


def _natural_add_intent(text: str) -> bool:
    if re.search(r"(?:不要|不用|先不|无需|别)\s*(?:添加|下载|订阅|追番|加入)", text):
        return False
    return bool(
        re.search(
            r"(?:添加|下载|订阅|追番|加入).{1,80}(?:ani|ani-rss|anirss|nas|rss|订阅|番剧|动画|字幕组)?",
            text,
            re.IGNORECASE,
        )
        and not re.search(r"(?:查看|列出|显示|搜索|查找|查一下|找一下|搜一下|刷新|更新)", text),
    )


def _natural_search_intent(text: str) -> bool:
    return bool(
        re.search(
            r"(?:搜索|查找|查一下|找一下|搜一下).{0,80}(?:mikan|蜜柑|字幕组|番剧|动画|rss|有没有字幕组)",
            text,
            re.IGNORECASE,
        )
        or re.search(r"有没有.{0,40}字幕组", text),
    )


def _natural_subscription_id(text: str) -> str:
    match = re.search(r"(?:id|ID|订阅)\s*[:：#]?\s*([A-Za-z0-9_-]{6,})", text)
    return match.group(1) if match else ""


def _natural_limit(text: str) -> int | None:
    patterns = (
        r"(?:limit|数量|条数)\s*=\s*(\d{1,2})",
        r"(?:前|推荐|列出|展示|显示|给我|来)\s*(\d{1,2})\s*(?:部|个|条|项)?",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            with suppress(ValueError):
                return max(int(match.group(1)), 1)
    return None


def _natural_min_score(text: str) -> float | None:
    match = re.search(
        r"(?:min_score|score|评分)\s*(?:>=|=|大于|超过|至少|不低于)?\s*(\d+(?:\.\d+)?)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    with suppress(ValueError):
        return max(float(match.group(1)), 0.0)
    return None


def _natural_season_params(text: str) -> dict[str, Any]:
    match = re.search(r"(?P<year>20\d{2})\s*(?P<season>[春夏秋冬])", text)
    if match:
        return {
            "year": int(match.group("year")),
            "season": match.group("season"),
        }
    return {}


def _extract_recommend_query(text: str) -> str:
    cleaned = re.sub(
        r"(列出|推荐|看看|看下|来点|有什么|有没有|当季|本季|当前季度|新番|番剧|动画|追番|推荐|筛选|列表|几部|一些|一下)",
        " ",
        text,
    )
    cleaned = re.sub(
        r"20\d{2}\s*[春夏秋冬]|limit\s*=\s*\d+|评分\s*[>=大于超过至少不低于=]*\s*\d+(?:\.\d+)?",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"\b\d{1,2}\s*(?:部|个|条|项)?\b", " ", cleaned)
    query = _clean_anime_query(cleaned)
    if query in {"部", "个", "条", "项", "以上", "以下"}:
        return ""
    return query


def _extract_add_query_and_destination(text: str) -> tuple[str, str]:
    url = _extract_url(text)
    if url:
        text = text.replace(url, " ")

    patterns = (
        r"^(?:帮我|给我|请|麻烦)?\s*(?:添加|下载|订阅|追番|加入)\s*(?P<query>.+?)(?:\s*(?:到|进|至|加入)\s*(?P<dest>.+))?$",
        r"^(?:帮我|给我|请|麻烦)?\s*(?:把|将)\s*(?P<query>.+?)\s*(?:添加|下载|订阅|追番|加入)(?:\s*(?:到|进|至|加入)\s*(?P<dest>.+))?$",
        r"^(?P<query>.+?)\s*(?:添加|下载|订阅|追番|加入)(?:\s*(?:到|进|至|加入)\s*(?P<dest>.+))?$",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        query = _clean_anime_query(match.group("query"))
        dest = str(match.groupdict().get("dest") or "").strip(" \t\r\n，。！？!?,;；")
        if query:
            return query, dest
    return "", ""


def _extract_search_query(text: str) -> str:
    url = _extract_url(text)
    if url:
        return url
    cleaned = re.sub(
        r"^(?:帮我|给我|请|麻烦)?\s*(?:搜索|查找|查一下|找一下|搜一下)\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"(?i)mikan|蜜柑|rss", " ", cleaned)
    cleaned = re.split(
        r"(?:，|,|。|；|;|\s)+(?:看看|看下|有没有|有哪些|有哪几|候选|字幕组|不要|不用|先不|无需|别)",
        cleaned,
        maxsplit=1,
    )[0]
    cleaned = re.sub(
        r"(有没有|有哪些|有哪几|字幕组|候选|番剧|动画|订阅|的)\s*$",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    return _clean_anime_query(cleaned)


def _clean_anime_query(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(" 「」『』《》<>[]【】'\"，。！？!?,;；")
    cleaned = re.sub(r"^(一下|帮我|给我|请|麻烦|把|将)\s*", "", cleaned)
    cleaned = re.sub(r"\s*(这部|这个)?(番剧|动画|新番|订阅|rss|RSS)\s*$", "", cleaned)
    return cleaned.strip(" 「」『』《》<>[]【】'\"，。！？!?,;；")


def _extract_url(text: str) -> str:
    match = re.search(r"https?://[^\s<>'\"，。！？]+", text)
    return match.group(0).rstrip("，。！？!?,;；") if match else ""
