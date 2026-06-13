from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import importlib.util
import io
import math
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from astrbot.api import logger

UI_ROOT = Path(__file__).resolve().parent
PLUGIN_LOGO_PATH = UI_ROOT.parent / "logo.png"
COVER_CACHE: dict[str, str] = {}
CARD_LOGO_DATA_URI = ""
MIKAN_ICON_DATA_URI = ""
MIKAN_CARD_HEADINGS = ("Mikan 搜索结果", "Mikan 推荐候选", "Mikan 选择要订阅的番剧")
MIKAN_SEARCH_HEADINGS = ("Mikan 搜索结果", "Mikan 选择要订阅的番剧")


def render_card_png(
    text: str,
    *,
    output_dir: str = "",
    width: int = 860,
    scale: int = 2,
    title: str = "",
    cover_proxy: dict[str, Any] | None = None,
) -> str:
    _setup_pylitehtml_fonts()
    from pylitehtml import FontConfig, html_to_png

    output_root = Path(output_dir).expanduser() if output_dir else _default_output_dir()
    output_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    path = output_root / f"ani_rss_card_{int(time.time())}_{digest}.png"
    render_width = max(int(width), 420)
    is_mikan_card = _is_mikan_card_text(text, title=title)
    is_mikan_group = _is_mikan_group_text(text, title=title)
    if is_mikan_card:
        render_width = _mikan_render_width(text, title=title, requested_width=render_width)
    elif is_mikan_group:
        render_width = 500
    render_scale = 1 if is_mikan_card or is_mikan_group else max(int(scale), 1)
    document = build_card_html(text, title=title, cover_proxy=cover_proxy)
    png = html_to_png(
        document,
        width=render_width,
        scale=render_scale,
        wrap=False,
        shrink_to_fit=True,
        fonts=FontConfig(default="sans-serif"),
    )
    path.write_bytes(png)
    return str(path)


async def result_for_interaction(plugin: Any, event: Any, text: str, *, title: str = "") -> tuple[Any, str]:
    mode = plugin.render_mode()
    if mode == "text":
        return event.plain_result(text), ""

    try:
        cover_proxy = {}
        cover_proxy_config = getattr(plugin, "cover_proxy_config", None)
        if callable(cover_proxy_config):
            cover_proxy = cover_proxy_config()
        path = await asyncio.to_thread(
            render_card_png,
            text,
            output_dir=plugin.render_output_dir(),
            width=plugin.render_width(),
            scale=plugin.render_scale(),
            title=title,
            cover_proxy=cover_proxy,
        )
    except Exception as exc:
        logger.warning("ANI-RSS card render failed, fallback to text: %s", exc)
        return event.plain_result(text), ""

    if mode == "both":
        return event.make_result().file_image(path).message("\n" + compact_caption(text)), path
    return event.image_result(path), path


def cleanup_rendered_cards(paths: list[str]) -> None:
    while paths:
        path = paths.pop()
        if not path:
            continue
        try:
            Path(path).unlink(missing_ok=True)
        except Exception as exc:
            logger.debug("Failed to remove ANI-RSS rendered card %s: %s", path, exc)


def compact_caption(text: str, *, limit: int = 180) -> str:
    lines = []
    for line in text.splitlines():
        clean = line.strip()
        if not clean or clean.startswith("|---"):
            continue
        if clean.startswith("|"):
            continue
        lines.append(clean)
        if len(" ".join(lines)) >= limit:
            break
    caption = " ".join(lines)
    return caption[:limit].rstrip()


def _source_icon_data_uri(lines: list[str], heading: str) -> str:
    text = "\n".join([heading, *lines]).lower()
    data_uri = _mikan_icon_data_uri() if heading.startswith("Mikan") or "mikan" in text else ""
    data_uri = data_uri or _card_logo_data_uri()
    if not data_uri:
        return '<div class="source-icon"></div>'
    alt = "Mikan" if heading.startswith("Mikan") or "mikan" in text else "ANI-RSS"
    return (
        '<img class="source-icon" '
        'style="width:42px;height:42px;max-width:42px;max-height:42px;" '
        f'src="{data_uri}" alt="{alt}">'
    )


def _mikan_icon_data_uri() -> str:
    global MIKAN_ICON_DATA_URI
    if MIKAN_ICON_DATA_URI:
        return MIKAN_ICON_DATA_URI
    try:
        from PIL import Image, ImageDraw

        size = 84
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, 80, 80), radius=18, fill="#ffd24a", outline="#f5b400", width=3)
        draw.ellipse((16, 16, 68, 68), fill="#ffc928", outline="#ffffff", width=5)
        center = (42, 42)
        for angle in range(0, 360, 45):
            radians = math.radians(angle)
            end = (42 + int(25 * math.cos(radians)), 42 + int(25 * math.sin(radians)))
            draw.line((center, end), fill="#ffffff", width=4)
        draw.ellipse((36, 36, 48, 48), fill="#ffffff")
        image = image.resize((42, 42), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        MIKAN_ICON_DATA_URI = f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"
    except (ImportError, OSError, ValueError) as exc:
        logger.debug("Failed to build Mikan card icon: %s", exc)
        MIKAN_ICON_DATA_URI = ""
    return MIKAN_ICON_DATA_URI


def _card_logo_data_uri() -> str:
    global CARD_LOGO_DATA_URI
    if CARD_LOGO_DATA_URI:
        return CARD_LOGO_DATA_URI
    try:
        from PIL import Image

        image = Image.open(PLUGIN_LOGO_PATH).convert("RGBA")
        bbox = image.getchannel("A").getbbox()
        if bbox:
            image = image.crop(bbox)
        side = max(image.size)
        padding = max(int(side * 0.08), 6)
        canvas = Image.new("RGBA", (side + padding * 2, side + padding * 2), (0, 0, 0, 0))
        canvas.alpha_composite(image, ((canvas.width - image.width) // 2, (canvas.height - image.height) // 2))
        canvas.thumbnail((42, 42))
        icon = Image.new("RGBA", (42, 42), (0, 0, 0, 0))
        icon.alpha_composite(canvas, ((42 - canvas.width) // 2, (42 - canvas.height) // 2))
        buffer = io.BytesIO()
        icon.save(buffer, format="PNG")
        CARD_LOGO_DATA_URI = f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"
    except (ImportError, OSError, ValueError) as exc:
        logger.debug("Failed to load ANI-RSS card logo %s: %s", PLUGIN_LOGO_PATH, exc)
        CARD_LOGO_DATA_URI = ""
    return CARD_LOGO_DATA_URI


def _is_mikan_card_text(text: str, *, title: str = "") -> bool:
    lines = [line.rstrip() for line in str(text or "").strip().splitlines()]
    heading, _, _ = _extract_heading(lines, title=title)
    return heading.startswith(MIKAN_CARD_HEADINGS)


def _is_mikan_group_text(text: str, *, title: str = "") -> bool:
    lines = [line.rstrip() for line in str(text or "").strip().splitlines()]
    heading, _, _ = _extract_heading(lines, title=title)
    return heading.startswith("Mikan 字幕组 RSS")


def _mikan_render_width(text: str, *, title: str = "", requested_width: int = 860) -> int:
    lines = [line.rstrip() for line in str(text or "").strip().splitlines()]
    heading, _, _ = _extract_heading(lines, title=title)
    if heading.startswith("Mikan 推荐候选"):
        return 680
    if heading.startswith(MIKAN_SEARCH_HEADINGS):
        return 580
    else:
        return requested_width


def _build_mikan_modal_document(
    heading: str,
    subtitle: str,
    body_lines: list[str],
    *,
    cover_proxy: dict[str, Any] | None = None,
) -> str:
    if not heading.startswith(MIKAN_CARD_HEADINGS):
        return ""

    rows, notice, prompt = _parse_mikan_rows(body_lines)
    if not rows:
        return ""

    scope = _mikan_scope_text(heading, subtitle, rows)
    layout = "recommend" if heading.startswith("Mikan 推荐候选") else "search"
    section_html = _render_mikan_card_section(
        scope,
        rows[:10],
        cover_proxy=cover_proxy,
        layout=layout,
    )
    notice_html = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
    prompt_html = f'<div class="prompt">{_inline_markup(prompt)}</div>' if prompt else ""
    subtitle_text = subtitle or f"{len(rows)} 项"
    eyebrow = "Mikan Search" if heading.startswith(MIKAN_SEARCH_HEADINGS) else "Mikan Recommend"
    source_icon = _source_icon_data_uri([], heading)

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      padding: 0;
      background: #f5f7fa;
      color: #303133;
      font-family: "PingFang SC", "Noto Sans SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
      font-size: 15px;
      line-height: 1.45;
    }}
    .page {{
      padding: 8px;
      width: 100%;
    }}
    .panel {{
      background: #ffffff;
      border: 1px solid #ebeef5;
      border-radius: 8px;
      box-shadow: 0 4px 12px rgba(31, 45, 61, 0.05);
      overflow: hidden;
    }}
    .header {{
      padding: 10px 12px 8px 12px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid #ebeef5;
    }}
    .header-left {{
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }}
    .source-icon {{
      flex: 0 0 42px;
      border-radius: 8px;
      background: #fff8df;
    }}
    .title-block {{
      min-width: 0;
    }}
    .eyebrow {{
      color: #409eff;
      font-size: 12px;
      font-weight: 700;
    }}
    h1 {{
      margin: 2px 0 0 0;
      color: #303133;
      font-size: 20px;
      line-height: 1.2;
      font-weight: 750;
    }}
    .summary {{
      color: #909399;
      font-size: 13px;
    }}
    .body {{
      padding: 8px 10px 10px 10px;
    }}
    .section {{
      margin-bottom: 10px;
    }}
    .section:last-child {{
      margin-bottom: 0;
    }}
    .section-title {{
      margin: 0 0 6px 0;
      font-size: 16px;
      line-height: 1.2;
      font-weight: 750;
      color: #111827;
    }}
    .grid {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: stretch;
      justify-content: flex-start;
    }}
    .grid.recommend .anime-card {{
      width: 316px;
      flex: 0 0 316px;
    }}
    .result-list {{
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .search-row {{
      position: relative;
      display: flex;
      gap: 14px;
      align-items: center;
      min-height: 122px;
      padding: 10px 80px 10px 10px;
      border: 1px solid #ebeef5;
      border-radius: 8px;
      background: #ffffff;
      box-shadow: 0 3px 8px rgba(31, 45, 61, 0.035);
    }}
    .search-row .anime-cover,
    .search-row .poster-placeholder {{
      width: 78px;
      height: 108px;
      border-radius: 6px;
    }}
    .search-main {{
      min-width: 0;
      flex: 1;
      padding-left: 4px;
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 5px;
    }}
    .search-title {{
      color: #303133;
      font-size: 14px;
      line-height: 1.32;
      font-weight: 750;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .search-meta {{
      position: absolute;
      right: 16px;
      top: 39px;
      width: 56px;
      text-align: right;
    }}
    .search-meta .tag {{
      margin-top: 4px;
    }}
    .search-score {{
      display: inline-block;
      color: #e800a4;
      font-size: 12px;
      line-height: 1.25;
      font-weight: 800;
    }}
    .search-details {{
      color: #909399;
      font-size: 11px;
      line-height: 1.35;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .search-summary {{
      color: #606266;
      font-size: 12px;
      line-height: 1.38;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .search-index {{
      position: absolute;
      right: 9px;
      top: 9px;
      width: 24px;
      height: 24px;
      border-radius: 12px;
      background: #ecf5ff;
      color: #409eff;
      font-size: 12px;
      font-weight: 800;
      line-height: 24px;
      text-align: center;
    }}
    .anime-card {{
      position: relative;
      width: 100%;
      flex: 0 0 100%;
      min-height: 90px;
      display: flex;
      gap: 8px;
      padding: 7px;
      border: 1px solid #ebeef5;
      border-radius: 8px;
      background: #ffffff;
      box-shadow: 0 4px 10px rgba(31, 45, 61, 0.04);
    }}
    .anime-cover {{
      width: 54px;
      height: 76px;
      border-radius: 6px;
      object-fit: cover;
      flex: 0 0 auto;
      background: #e9edf4;
    }}
    .poster-placeholder {{
      width: 54px;
      height: 76px;
      border-radius: 6px;
      flex: 0 0 auto;
      background: #f0f2f5;
      color: #909399;
      font-size: 18px;
      font-weight: 800;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .anime-main {{
      min-width: 0;
      flex: 1;
      padding-right: 24px;
    }}
    .index {{
      position: absolute;
      right: 7px;
      top: 7px;
      width: 20px;
      height: 20px;
      border-radius: 10px;
      background: #ecf5ff;
      color: #409eff;
      font-size: 12px;
      font-weight: 800;
      line-height: 20px;
      text-align: center;
    }}
    .anime-title {{
      margin: 1px 0 1px 0;
      color: #303133;
      font-size: 14px;
      line-height: 1.35;
      font-weight: 750;
    }}
    .score {{
      color: #e800a4;
      font-size: 17px;
      font-weight: 800;
      margin-bottom: 4px;
    }}
    .tags {{
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
    }}
    .tag {{
      display: inline-block;
      min-width: 42px;
      padding: 2px 6px;
      border-radius: 6px;
      background: #f4f4f5;
      color: #909399;
      font-size: 11px;
      line-height: 1.4;
      text-align: center;
    }}
    .tag.week {{
      background: #ecf5ff;
      color: #409eff;
    }}
    .tag.available {{
      background: #f0f9eb;
      color: #67c23a;
    }}
    .tag.subscribed {{
      background: #ecf5ff;
      color: #409eff;
    }}
    .notice {{
      margin-top: 8px;
      padding: 7px 9px;
      background: #f4f4f5;
      color: #909399;
      border-radius: 6px;
      font-size: 12px;
    }}
    .prompt {{
      margin-top: 8px;
      padding: 8px 10px;
      background: #ecf5ff;
      color: #337ecc;
      border: 1px solid #d9ecff;
      border-radius: 8px;
      font-weight: 600;
      font-size: 12px;
    }}
    .badge {{
      display: inline-block;
      padding: 2px 7px;
      border-radius: 6px;
      background: #ecf5ff;
      color: #409eff;
      font-size: 12px;
      font-weight: 600;
      line-height: 1.35;
    }}
    .badge.success {{
      background: #f0f9eb;
      color: #67c23a;
    }}
    .badge.danger {{
      background: #fef0f0;
      color: #f56c6c;
    }}
  </style>
</head>
<body>
  <div class="page">
      <div class="panel">
        <div class="header">
        <div class="header-left">
          {source_icon}
          <div class="title-block">
            <div class="eyebrow">{html.escape(eyebrow)}</div>
            <h1>{html.escape(heading)}</h1>
          </div>
        </div>
        <div class="summary">{html.escape(subtitle_text)}</div>
      </div>
      <div class="body">
        {section_html}
        {notice_html}
        {prompt_html}
      </div>
    </div>
  </div>
</body>
</html>"""


def _parse_mikan_rows(body_lines: list[str]) -> tuple[list[dict[str, str]], str, str]:
    rows: list[dict[str, str]] = []
    notice = ""
    prompt_lines: list[str] = []
    in_table = False
    skipped_separator = False

    for raw in body_lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("| # |"):
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            skipped_separator = True
            continue
        if in_table and skipped_separator and line.startswith("|"):
            cells = _split_table_row(line)
            if len(cells) >= 5:
                rows.append(
                    {
                        "index": cells[0],
                        "score": cells[1],
                        "status": cells[2],
                        "week": cells[3],
                        "title": cells[4],
                        "cover": "",
                        "url": "",
                    },
                )
            continue
        if rows and line.startswith("cover:"):
            rows[-1]["cover"] = line.partition(":")[2].strip()
            continue
        if rows and line.startswith("meta:"):
            rows[-1]["meta"] = line.partition(":")[2].strip()
            continue
        if rows and line.startswith("summary:"):
            rows[-1]["summary"] = line.partition(":")[2].strip()
            continue
        if rows and line.startswith(("http://", "https://")):
            rows[-1]["url"] = line
            continue
        if line.startswith("评分来自"):
            notice = line
            continue
        if line.startswith(("回复", "任务ID:", "状态:")):
            prompt_lines.append(line)
            continue
        if rows:
            prompt_lines.append(line)

    prompt = " ".join(prompt_lines)
    return rows, notice, prompt


def _group_mikan_rows(
    rows: list[dict[str, str]],
    *,
    fallback: str,
) -> list[tuple[str, list[dict[str, str]]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    order: list[str] = []
    for row in rows:
        label = (row.get("week") or "").strip()
        if not label or label == "-":
            label = fallback or "搜索结果"
        if label not in grouped:
            grouped[label] = []
            order.append(label)
        grouped[label].append(row)
    return [(label, grouped[label]) for label in order]


def _render_mikan_card_section(
    label: str,
    rows: list[dict[str, str]],
    *,
    cover_proxy: dict[str, Any] | None = None,
    layout: str = "search",
) -> str:
    title = "搜索结果" if label == "Search" else label
    if layout == "search":
        rows_html = "\n".join(
            _render_mikan_candidate_list_row(row, cover_proxy=cover_proxy)
            for row in rows
        )
        return (
            '<div class="section search-section">'
            f'<div class="result-list" aria-label="{html.escape(title)}">'
            f"{rows_html}"
            "</div>"
            "</div>"
        )

    cards = "\n".join(
        _render_mikan_candidate_card(row, cover_proxy=cover_proxy)
        for row in rows
    )
    return (
        '<div class="section">'
        f'<h2 class="section-title">{html.escape(title)}</h2>'
        f'<div class="grid {html.escape(layout)}">'
        f"{cards}"
        "</div>"
        "</div>"
    )


def _render_mikan_candidate_list_row(
    row: dict[str, str],
    *,
    cover_proxy: dict[str, Any] | None = None,
) -> str:
    title = row.get("title") or "(untitled)"
    score = row.get("score") or "-"
    score_html = f'<span class="search-score">评分 {html.escape(score)}</span>' if score != "-" else ""
    status = row.get("status") or "可订阅"
    status_class = "subscribed" if status == "已订阅" else "available"
    week = row.get("week") or ""
    if week == "-":
        week = "Mikan"
    meta = _short_text(row.get("meta") or "", 42)
    summary = _short_text(row.get("summary") or "", 96)
    meta_html = f'<div class="search-details">{html.escape(meta)}</div>' if meta else ""
    summary_html = f'<div class="search-summary">{html.escape(summary)}</div>' if summary else ""
    cover = _candidate_cover_html(
        row.get("cover") or "",
        row.get("title") or "",
        row.get("index") or "",
        cover_proxy=cover_proxy,
    )
    index = html.escape(str(row.get("index") or "?")[:2])
    return (
        '<div class="search-row">'
        f"{cover}"
        '<div class="search-main">'
        f'<div class="search-title">{html.escape(title)}</div>'
        f"{meta_html}"
        f"{summary_html}"
        "</div>"
        '<div class="search-meta">'
        f"{score_html}"
        "<br>"
        f'<span class="tag {status_class}">{html.escape(status)}</span>'
        "<br>"
        f'<span class="tag week">{html.escape(week)}</span>'
        "</div>"
        f'<div class="search-index">{index}</div>'
        "</div>"
    )


def _render_mikan_candidate_card(
    row: dict[str, str],
    *,
    cover_proxy: dict[str, Any] | None = None,
) -> str:
    title = _short_text(row.get("title") or "(untitled)", 28)
    score = row.get("score") or "-"
    score_html = f'<div class="score">{html.escape(score)}</div>' if score != "-" else '<div class="score">-</div>'
    status = row.get("status") or "可订阅"
    status_class = "subscribed" if status == "已订阅" else "available"
    week = row.get("week") or ""
    if week == "-":
        week = "Mikan"
    cover = _candidate_cover_html(
        row.get("cover") or "",
        row.get("title") or "",
        row.get("index") or "",
        cover_proxy=cover_proxy,
    )
    index = html.escape(str(row.get("index") or "?")[:2])
    return (
        '<div class="anime-card">'
        f"{cover}"
        f'<div class="index">{index}</div>'
        '<div class="anime-main">'
        f'<div class="anime-title">{html.escape(title)}</div>'
        f"{score_html}"
        '<div class="tags">'
        f'<span class="tag week">{html.escape(week)}</span>'
        f'<span class="tag {status_class}">{html.escape(status)}</span>'
        '<span class="tag">Mikan</span>'
        "</div>"
        "</div>"
        "</div>"
    )


def _candidate_cover_html(
    url: str,
    title: str,
    index: str,
    *,
    cover_proxy: dict[str, Any] | None = None,
) -> str:
    src = _cover_data_uri(url, cover_proxy=cover_proxy)
    if src:
        return f'<img class="anime-cover" src="{src}" alt="{html.escape(title)}">'
    label = html.escape(str(index or "?")[:2])
    return f'<div class="poster-placeholder">{label}</div>'


def _render_mikan_week_section(
    label: str,
    rows: list[dict[str, str]],
    *,
    cover_proxy: dict[str, Any] | None = None,
) -> str:
    row_html = "\n".join(
        _render_mikan_modal_row(row, cover_proxy=cover_proxy)
        for row in rows
    )
    return (
        '<div class="week-section">'
        '<div class="week-header">'
        '<span class="week-chevron">v</span>'
        f'<span class="week-title">{html.escape(label)}</span>'
        "</div>"
        '<div class="collapse-content">'
        '<div class="list">'
        f"{row_html}"
        "</div>"
        "</div>"
        "</div>"
    )


def _mikan_scope_text(heading: str, subtitle: str, rows: list[dict[str, str]]) -> str:
    if "（" in subtitle and "）" in subtitle:
        return subtitle.split("（", 1)[1].split("）", 1)[0].strip() or "当前季度"
    for row in rows:
        week = row.get("week", "")
        if week and week not in {"-", "Search"}:
            return "当前筛选"
    if heading.startswith("Mikan 选择要订阅的番剧"):
        return "选择要订阅的番剧"
    if heading.startswith("Mikan 搜索结果"):
        return "搜索结果"
    return "当前季度"


def _render_mikan_modal_row(
    row: dict[str, str],
    *,
    cover_proxy: dict[str, Any] | None = None,
) -> str:
    title = _short_text(row.get("title") or "(untitled)", 48)
    score = row.get("score") or "-"
    score_html = f'<span class="score">{html.escape(score)}</span>' if score != "-" else ""
    badge = '<span class="subscribed">已订阅</span>' if row.get("status") == "已订阅" else ""
    cover = _cover_html(
        row.get("cover") or "",
        row.get("title") or "",
        row.get("index") or "",
        cover_proxy=cover_proxy,
    )
    return (
        '<div class="mikan-row">'
        f"{cover}"
        '<div class="row-main">'
        f'<span class="row-title">{html.escape(title)}</span>'
        f"{score_html}{badge}"
        "</div>"
        '<div class="arrow">&gt;</div>'
        "</div>"
    )


def _cover_html(
    url: str,
    title: str,
    index: str,
    *,
    cover_proxy: dict[str, Any] | None = None,
) -> str:
    src = _cover_data_uri(url, cover_proxy=cover_proxy)
    if src:
        return f'<img class="cover" src="{src}" alt="{html.escape(title)}">'
    label = html.escape(str(index or "?")[:2])
    return f'<div class="cover-placeholder">{label}</div>'


def _cover_data_uri(url: str, *, cover_proxy: dict[str, Any] | None = None) -> str:
    clean = str(url or "").strip()
    if not clean.startswith(("http://", "https://")):
        return ""
    cache_key = _cover_cache_key(clean, cover_proxy)
    if cache_key in COVER_CACHE:
        return COVER_CACHE[cache_key]
    try:
        raw = _fetch_cover_bytes(clean, cover_proxy=cover_proxy)
        from PIL import Image

        image = Image.open(io.BytesIO(raw)).convert("RGB")
        image.thumbnail((96, 96))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        data_uri = f"data:image/png;base64,{encoded}"
    except (ImportError, OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        logger.debug("Failed to load Mikan cover %s: %s", clean, exc)
        data_uri = ""
    COVER_CACHE[cache_key] = data_uri
    return data_uri


def _cover_cache_key(url: str, cover_proxy: dict[str, Any] | None) -> str:
    if not cover_proxy:
        return url
    base_url = str(cover_proxy.get("base_url") or "").rstrip("/")
    api_prefix = str(cover_proxy.get("api_prefix") or "").strip().rstrip("/")
    auth_marker = "auth" if str(cover_proxy.get("api_key") or "").strip() else "anon"
    return f"{url}\0{base_url}{api_prefix}\0{auth_marker}"


def _fetch_cover_bytes(url: str, *, cover_proxy: dict[str, Any] | None = None) -> bytes:
    errors: list[Exception] = []
    for request in _cover_requests(url, cover_proxy=cover_proxy):
        try:
            timeout = _cover_timeout(cover_proxy)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(2_000_000)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            errors.append(exc)
    if errors:
        raise errors[-1]
    raise ValueError("No cover request available.")


def _cover_requests(
    url: str,
    *,
    cover_proxy: dict[str, Any] | None = None,
) -> list[urllib.request.Request]:
    headers = {"User-Agent": "AstrBot ANI-RSS Card"}
    requests: list[urllib.request.Request] = []
    proxy_url = _cover_proxy_url(url, cover_proxy)
    if proxy_url:
        proxy_headers = dict(headers)
        api_key = str((cover_proxy or {}).get("api_key") or "").strip()
        if api_key:
            proxy_headers["api-key"] = api_key
        requests.append(urllib.request.Request(proxy_url, headers=proxy_headers))
    requests.append(urllib.request.Request(url, headers=headers))
    return requests


def _cover_proxy_url(url: str, cover_proxy: dict[str, Any] | None) -> str:
    if not cover_proxy:
        return ""
    base_url = str(cover_proxy.get("base_url") or "").strip().rstrip("/")
    api_prefix = str(cover_proxy.get("api_prefix") or "/api").strip()
    if api_prefix and not api_prefix.startswith("/"):
        api_prefix = f"/{api_prefix}"
    api_prefix = api_prefix.rstrip("/")
    if not base_url:
        return ""
    encoded = base64.b64encode(url.encode("utf-8")).decode("ascii")
    return f"{base_url}{api_prefix}/proxyImage?imgUrl={urllib.parse.quote(encoded)}"


def _cover_timeout(cover_proxy: dict[str, Any] | None) -> int:
    try:
        return min(max(int((cover_proxy or {}).get("timeout_seconds", 5)), 1), 20)
    except (TypeError, ValueError):
        return 5


def _short_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)].rstrip() + "…"


def build_card_html(
    text: str,
    *,
    title: str = "",
    cover_proxy: dict[str, Any] | None = None,
) -> str:
    text = text.strip()
    lines = [line.rstrip() for line in text.splitlines()]
    heading, subtitle, body_lines = _extract_heading(lines, title=title)
    mikan_modal = _build_mikan_modal_document(
        heading,
        subtitle,
        body_lines,
        cover_proxy=cover_proxy,
    )
    if mikan_modal:
        return mikan_modal
    blocks = _render_blocks(body_lines)
    icon = _source_icon_data_uri(lines, heading)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      padding: 0;
      background: #f5f7fa;
      color: #303133;
      font-family: "PingFang SC", "Noto Sans SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
      font-size: 15px;
      line-height: 1.55;
    }}
    .page {{
      padding: 22px;
      width: 100%;
    }}
    .card {{
      background: #ffffff;
      border: 1px solid #ebeef5;
      border-radius: 12px;
      box-shadow: 0 4px 8px rgba(0, 0, 0, 0.04);
      overflow: hidden;
    }}
    .header {{
      padding: 18px 22px 14px 22px;
      border-bottom: 1px solid #ebeef5;
      display: flex;
      align-items: center;
      gap: 14px;
    }}
    .source-icon {{
      width: 42px;
      height: 42px;
      border-radius: 12px;
      background: #ecf5ff;
      border: 1px solid #d9ecff;
      padding: 7px;
      object-fit: contain;
    }}
    .header-text {{
      min-width: 0;
      flex: 1;
    }}
    .eyebrow {{
      color: #409eff;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    h1 {{
      margin: 3px 0 2px 0;
      font-size: 24px;
      line-height: 1.25;
      font-weight: 650;
      color: #303133;
    }}
    .subtitle {{
      color: #909399;
      font-size: 14px;
    }}
    .body {{
      padding: 18px 22px 20px 22px;
    }}
    p {{
      margin: 8px 0;
    }}
    .notice {{
      margin-top: 10px;
      padding: 10px 12px;
      background: #f4f4f5;
      border: 1px solid #e9e9eb;
      border-radius: 8px;
      color: #606266;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .prompt {{
      margin-top: 12px;
      padding: 10px 12px;
      background: #ecf5ff;
      border: 1px solid #d9ecff;
      border-radius: 8px;
      color: #337ecc;
      font-weight: 650;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 8px;
      margin-bottom: 10px;
      border: 1px solid #ebeef5;
      border-radius: 8px;
      overflow: hidden;
    }}
    th {{
      background: #f5f7fa;
      color: #606266;
      text-align: left;
      font-weight: 650;
      padding: 10px 12px;
      border-bottom: 1px solid #ebeef5;
    }}
    td {{
      padding: 10px 12px;
      border-bottom: 1px solid #ebeef5;
      vertical-align: top;
    }}
    tbody tr:nth-child(even) {{
      background: #fafafa;
    }}
    tr:last-child td {{
      border-bottom: none;
    }}
    .num {{
      color: #409eff;
      font-weight: 650;
      white-space: nowrap;
    }}
    .score {{
      color: #e800a4;
      font-weight: 750;
      white-space: nowrap;
    }}
    .muted {{
      color: #909399;
      font-size: 13px;
    }}
    .line {{
      padding: 8px 0;
      border-bottom: 1px solid #f2f6fc;
      color: #606266;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .line:last-child {{
      border-bottom: none;
    }}
    .url {{
      margin: 4px 0 8px 0;
      color: #909399;
      font-size: 12px;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .url-label {{
      color: #909399;
      font-weight: 650;
    }}
    .url-text {{
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .sample {{
      padding: 5px 0;
      color: #606266;
      font-size: 13px;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .badge {{
      display: inline-block;
      padding: 2px 7px;
      border-radius: 6px;
      background: #ecf5ff;
      color: #409eff;
      font-size: 12px;
      font-weight: 600;
      line-height: 1.35;
    }}
    .badge.success {{
      background: #f0f9eb;
      color: #67c23a;
    }}
    .badge.info {{
      background: #f4f4f5;
      color: #909399;
    }}
    .badge.warning {{
      background: #fdf6ec;
      color: #e6a23c;
    }}
    .badge.danger {{
      background: #fef0f0;
      color: #f56c6c;
    }}
    .path {{
      color: #606266;
      background: #f5f7fa;
      border-radius: 8px;
      padding: 8px 10px;
      margin: 8px 0;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .item-row {{
      display: flex;
      gap: 8px;
      align-items: flex-start;
      padding: 8px 0;
      border-bottom: 1px solid #f2f6fc;
    }}
    .item-row:last-child {{
      border-bottom: none;
    }}
    .item-index {{
      display: inline-block;
      min-width: 22px;
      height: 22px;
      line-height: 22px;
      text-align: center;
      border-radius: 50%;
      background: #ecf5ff;
      color: #409eff;
      font-size: 12px;
      font-weight: 650;
      flex: 0 0 auto;
    }}
    .item-text {{
      flex: 1;
      min-width: 0;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .footer {{
      padding: 11px 22px 15px 22px;
      color: #909399;
      font-size: 12px;
      border-top: 1px solid #ebeef5;
    }}
  </style>
</head>
<body>
  <div class="page">
    <div class="card">
      <div class="header">
        {icon}
        <div class="header-text">
          <div class="eyebrow">ANI-RSS</div>
          <h1>{html.escape(heading)}</h1>
          <div class="subtitle">{html.escape(subtitle)}</div>
        </div>
      </div>
      <div class="body">
        {blocks}
      </div>
      <div class="footer">交互完成、取消或超时后会自动清理本次生成的临时卡片。</div>
    </div>
  </div>
</body>
</html>"""


def _extract_heading(lines: list[str], *, title: str = "") -> tuple[str, str, list[str]]:
    if title:
        return title, "", lines
    if not lines:
        return "ANI-RSS", "", []
    first = lines[0].strip()
    heading = first
    subtitle = ""
    if ":" in first:
        heading, subtitle = first.split(":", 1)
        heading = heading.strip() or "ANI-RSS"
        subtitle = subtitle.strip()
    return heading, subtitle, lines[1:]


def _render_blocks(lines: list[str]) -> str:
    html_blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if _is_table_start(lines, index):
            table_lines = [lines[index], lines[index + 1]]
            index += 2
            while index < len(lines):
                current = lines[index].strip()
                if current.startswith("|"):
                    table_lines.append(lines[index])
                    index += 1
                    continue
                if current.startswith(("http://", "https://")):
                    index += 1
                    continue
                break
            html_blocks.append(_render_table(table_lines))
            continue
        if line.startswith("回复"):
            html_blocks.append(f'<div class="prompt">{html.escape(line)}</div>')
        elif line.startswith("评分来自") or line.startswith("当前预览"):
            html_blocks.append(f'<div class="notice">{html.escape(line)}</div>')
        elif line.startswith(("http://", "https://", "rss:")):
            html_blocks.append(f'<div class="url">{_breakable_url_markup(line)}</div>')
        elif line.startswith(("sample:", "示例:")):
            html_blocks.append(f'<div class="sample">{_inline_markup(line)}</div>')
        elif line.startswith(("下载路径:", "路径:")):
            html_blocks.append(f'<div class="path">{_inline_markup(line)}</div>')
        elif _looks_numbered(line):
            html_blocks.append(_render_numbered_line(line))
        else:
            html_blocks.append(f'<div class="line">{_inline_markup(line)}</div>')
        index += 1
    return "\n".join(html_blocks)


def _is_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    return lines[index].strip().startswith("|") and set(lines[index + 1].strip()) <= {
        "|",
        "-",
        ":",
        " ",
    }


def _render_table(lines: list[str]) -> str:
    header = _split_table_row(lines[0])
    rows = [_split_table_row(line) for line in lines[2:]]
    header_html = "".join(f"<th>{html.escape(cell)}</th>" for cell in header)
    row_html = []
    for row in rows:
        cells = []
        for idx, cell in enumerate(row):
            cls = ""
            if idx == 0:
                cls = ' class="num"'
            elif idx == 1:
                cls = ' class="score"'
            cells.append(f"<td{cls}>{_table_cell(cell)}</td>")
        row_html.append("<tr>" + "".join(cells) + "</tr>")
    return "<table><thead><tr>" + header_html + "</tr></thead><tbody>" + "".join(row_html) + "</tbody></table>"


def _split_table_row(line: str) -> list[str]:
    return [part.strip() for part in line.strip().strip("|").split("|")]


def _looks_numbered(line: str) -> bool:
    prefix, _, rest = line.partition(".")
    return bool(rest.strip()) and prefix.isdigit()


def _render_numbered_line(line: str) -> str:
    prefix, _, rest = line.partition(".")
    return (
        '<div class="item-row">'
        f'<span class="item-index">{html.escape(prefix)}</span>'
        f'<div class="item-text">{_inline_markup(rest.strip())}</div></div>'
    )


def _table_cell(text: str) -> str:
    if text == "可订阅":
        return '<span class="badge success">可订阅</span>'
    if text == "已订阅":
        return '<span class="badge info">已订阅</span>'
    if text in {"enabled", "已启用"}:
        return f'<span class="badge success">{html.escape(text)}</span>'
    if text in {"disabled", "未启用"}:
        return f'<span class="badge info">{html.escape(text)}</span>'
    return _inline_markup(text)


def _inline_markup(text: str) -> str:
    escaped = html.escape(text)
    classes = {
        "可订阅": "success",
        "已订阅": "info",
        "enabled": "success",
        "disabled": "info",
        "确认": "success",
        "取消": "danger",
        "简中": "success",
        "简繁": "warning",
    }
    for word, badge_class in classes.items():
        escaped = escaped.replace(
            html.escape(word),
            f'<span class="badge {badge_class}">{html.escape(word)}</span>',
        )
    return escaped


def _breakable_url_markup(text: str) -> str:
    clean = str(text or "").strip()
    if clean.lower().startswith("rss:"):
        label, _, url = clean.partition(":")
        url = url.strip()
        if not url:
            return html.escape(clean)
        return (
            f'<span class="url-label">{html.escape(label)}:</span><br>'
            f'<span class="url-text">{_breakable_text(url)}</span>'
        )
    return f'<span class="url-text">{_breakable_text(clean)}</span>'


def _breakable_text(text: str, *, chunk_size: int = 18) -> str:
    parts: list[str] = []
    run = 0
    for char in str(text or ""):
        parts.append(html.escape(char))
        run += 1
        if char in "/?&=-_.#%" or run >= chunk_size:
            parts.append("<wbr>")
            run = 0
    return "".join(parts)


def _default_output_dir() -> Path:
    return Path(tempfile.gettempdir()) / "astrbot_plugin_ani_rss"


def _setup_pylitehtml_fonts() -> None:
    if os.environ.get("FONTCONFIG_FILE"):
        return
    spec = importlib.util.find_spec("pylitehtml")
    if not spec or not spec.origin:
        return
    fonts_dir = Path(spec.origin).resolve().parent / "fonts"
    fonts_conf = fonts_dir / "fonts.conf"
    if fonts_conf.exists():
        os.environ["FONTCONFIG_FILE"] = str(fonts_conf)
        os.environ.setdefault("FONTCONFIG_PATH", str(fonts_dir))
