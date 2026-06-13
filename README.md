# astrbot_plugin_ani_rss

AstrBot 的 ANI-RSS 订阅助手插件，用于对接 [wushuo894/ani-rss](https://github.com/wushuo894/ani-rss)。

当前版本：`1.0.0`

## 功能

- 通过一个 Agent 工具 `ani_rss` 处理订阅相关工作流。
- 支持自然语言添加番剧，例如“帮我添加淡岛百景到 ANI”。
- 支持 Mikan 搜索、季度推荐、番剧候选选择和字幕组选择。
- Mikan 流程选定字幕组后立即提交到 ANI-RSS，不再二次确认。
- RSS 直连添加会展示订阅摘要，用户确认后再写入 ANI-RSS。
- 支持查看订阅列表、刷新单个订阅和刷新全部订阅。
- 交互卡片默认渲染为 PNG；渲染失败会回退为文本。

## 安装

把本仓库放入 AstrBot 插件目录后，在 AstrBot WebUI 中启用插件并安装依赖。

插件依赖写在 `requirements.txt`：

```text
aiohttp
pylitehtml
Pillow
```

## 配置

在 AstrBot WebUI 的插件配置中填写：

- `base_url`: ANI-RSS 地址，例如 `http://127.0.0.1:7789`
- `api_prefix`: API 前缀，通常保持 `/api`
- `api_key`: ANI-RSS API Key
- `timeout_seconds`: 调用 ANI-RSS 接口的超时时间，默认 `30`
- `interaction_timeout_seconds`: 等待用户选择或确认的时间，默认 `120`
- `pending_task_ttl_seconds`: 挂起任务保留时间，默认 `86400`
- `render_mode`: 卡片发送模式，支持 `image` / `text` / `both`

不要把真实 API Key 写入仓库文件。

## 日常用法

用户不需要记 workflow 名称，直接用自然语言即可：

```text
列出当季推荐番剧
推荐 5 部本季新番，评分至少 7
帮我添加淡岛百景到 ANI
帮我下载淡岛百景到 NAS
查一下淡岛百景有没有字幕组
查看 ANI-RSS 订阅列表
刷新全部订阅
检查 ANI-RSS 连接状态
```

Mikan 添加流程：

1. 按番名搜索 Mikan。
2. 如果有多个番剧候选，用户选择番剧。
3. 用户选择字幕组。
4. 插件调用 ANI-RSS 的 `rssToAni` 和 `addAni`，直接创建订阅。

候选卡片会带 `任务ID`。用户可以引用卡片回复序号，也可以发送：

```text
/anirss 继续 <任务ID前后几位> <序号|取消>
```

命令前缀来自 AstrBot 全局 `wake_prefix`。如果唤醒词是 `plana`，命令写作：

```text
plana anirss 继续 <任务ID前后几位> <序号|取消>
```

`ANI`、`NAS` 只表示目标服务，不会被当成下载路径。下载目录、过滤规则和后续刷新由 ANI-RSS 处理。

## Workflow

用户可见的工作流：

- `add_mikan_subscription`: 按番名搜索 Mikan，选择番剧和字幕组后添加。
- `recommend_mikan_subscription`: 按季度或关键词推荐 Mikan 候选，选择番剧和字幕组后添加。
- `search_mikan`: 搜索 Mikan 番剧，或查看某个 Mikan 番剧页的字幕组 RSS。
- `add_subscription`: 从已知 RSS URL 构造订阅，用户确认后添加。
- `continue_pending`: 用完整任务 ID 或当前会话内不冲突的前后几位继续挂起流程。
- `list_subscriptions`: 查看订阅列表。
- `refresh_subscription`: 刷新单个订阅。
- `refresh_all`: 刷新全部订阅。

诊断工作流：

- `check_status`: 检查 ANI-RSS API 连通性。
- `preview_subscription`: 预览订阅结构，不进入日常添加流程。

## 命令

```text
/anirss help
/anirss add_subscription <rss_url>
/anirss add_mikan_subscription <番名>
/anirss recommend_mikan_subscription [番名|2026 春] [min_score=7] [limit=8]
/anirss search_mikan <番名或Mikan链接>
/anirss list_subscriptions [limit]
/anirss refresh_subscription <subscription_id>
/anirss refresh_all
/anirss 继续 <任务ID前后几位> <序号|确认|取消>
/anirss check_status
```

`确认` 只用于 RSS 直连添加；Mikan 字幕组选定后会直接提交。

## 项目结构

```text
astrbot_plugin_ani_rss/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── logo.png
├── requirements.txt
├── README.md
├── __init__.py
├── integrations/
│   ├── __init__.py
│   └── ani_rss.py
├── ui/
│   ├── __init__.py
│   └── rendering.py
├── workflows/
│   ├── __init__.py
│   ├── formatting.py
│   ├── mikan.py
│   ├── models.py
│   ├── parsing.py
│   ├── runner.py
│   └── utils.py
└── scripts/
    ├── __init__.py
    ├── api.py
    ├── rendering.py
    └── workflows.py
```

`main.py` 保留 AstrBot 插件入口、命令注册和工具注册；业务实现拆分到 `integrations/`、`workflows/` 和 `ui/`。`scripts/` 仅保留旧导入路径兼容层。

## API 调用

Mikan 添加会使用：

- `POST /api/mikan?text=<番名>`
- `POST /api/mikanGroup?url=<Mikan番剧页URL>`
- `POST /api/rssToAni`
- `POST /api/addAni`

RSS 直连添加会使用：

- `POST /api/rssToAni`
- `POST /api/addAni`

订阅管理会使用：

- `POST /api/listAni`
- `POST /api/refreshAni`
- `POST /api/refreshAll`

插件通过 `api-key` 请求头发送 ANI-RSS API Key。
