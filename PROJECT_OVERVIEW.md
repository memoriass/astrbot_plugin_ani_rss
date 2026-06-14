# astrbot_plugin_ani_rss

AstrBot 的 ANI-RSS 订阅助手插件。插件提供 `ani_rss` Agent 工具，让用户用自然语言搜索 Mikan、选择字幕组，并把 RSS 订阅提交到 ANI-RSS。

当前版本：`1.0.1`

## 能力

- 自然语言添加番剧订阅，例如“帮我订阅躲在超市后门抽烟的两人”。
- 支持 Mikan 搜索、季度推荐、番剧候选选择、字幕组选择。
- Mikan 字幕组选定后直接调用 ANI-RSS API 添加订阅。
- RSS 直连添加会先生成确认卡片，确认后再写入 ANI-RSS。
- 交互结果默认渲染为图片卡片；渲染失败会回退为文本。

## 配置

在 AstrBot WebUI 的插件配置中填写：

- `base_url`: ANI-RSS 服务地址，例如 `http://127.0.0.1:7789`
- `api_prefix`: ANI-RSS API 前缀，通常为 `/api`
- `api_key`: ANI-RSS API Key，通过 `api-key` 请求头发送
- `timeout_seconds`: API 请求超时，默认 `30`
- `interaction_timeout_seconds`: 用户交互等待时间，默认 `120`
- `pending_task_ttl_seconds`: 挂起任务保留时间，默认 `86400`
- `render_mode`: 卡片发送模式，`image` / `text` / `both`

不要把真实 API Key 写入仓库文件。

## 使用

用户优先使用自然语言：

```text
推荐几部本季新番
帮我订阅淡岛百景
查一下淡岛百景有没有字幕组
查看 ANI-RSS 订阅列表
刷新全部订阅
```

候选卡片会带任务 ID。继续挂起流程时使用：

```text
/ani<任务ID前后几位> <序号|确认|取消>
```

命令前缀来自 AstrBot 全局 `wake_prefix`。如果唤醒词是 `plana`，写作：

```text
plana ani<任务ID前后几位> <序号|确认|取消>
```

## 架构文档

根目录只保留 `main.py` 作为 AstrBot 插件入口，真实插件运行时在 `plugin/`；其他 Python 文件按职责归入 `routing/`、`integrations/`、`workflows/`、`ui/`、`scripts/`。

- `plugin/PLUGIN_RUNTIME.md`: AstrBot 插件类、配置、挂起任务和订阅构建。
- `COMMAND_ROUTING.md`: AstrBot 命令、wake prefix、短任务 ID 路由。
- `integrations/ANI_RSS_API.md`: ANI-RSS API 客户端和接口约束。
- `workflows/WORKFLOW_ARCHITECTURE.md`: 工作流解析、挂起任务和执行编排。
- `ui/CARD_RENDERING.md`: 图片卡片渲染结构。
- `scripts/COMPATIBILITY_LAYER.md`: 旧导入路径兼容层。

`requirements.txt` 记录运行依赖：`aiohttp`、`pylitehtml`、`Pillow`。
