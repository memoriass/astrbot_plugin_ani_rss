# Workflow Architecture

订阅工作流编排层。这里把自然语言、命令和 Agent 工具参数转换成具体 ANI-RSS 操作。

## 文件

- `models.py`: workflow 定义、别名、确认/取消回复集合。
- `parsing.py`: workflow request 解析统合入口。
- `parsing_tool.py`: LLM tool 参数解析。
- `parsing_command.py`: 显式命令和短任务命令解析。
- `parsing_natural.py`: 中文自然语言意图解析。
- `runner.py`: workflow 分发入口，保持轻量，不承载具体业务步骤。
- `runtime.py`: 回复封装、卡片交互回复、静默连通性预检、订阅参数构造。
- `rss.py`: RSS 直连添加、确认添加、订阅预览。
- `mikan_flow.py`: Mikan workflow 统合入口，只重新导出各子流程。
- `mikan_add.py`: Mikan 按番名添加入口。
- `mikan_recommend.py`: Mikan 推荐候选流程。
- `mikan_search.py`: Mikan 搜索和字幕组 RSS 查询。
- `mikan_selection.py`: 番剧选择、字幕组选择和续跑自动添加。
- `continuation.py`: 挂起任务续跑分发。
- `manage.py`: 订阅列表、单个刷新、全部刷新、连通性诊断。
- `pending.py`: 挂起任务、任务 ID 匹配、引用消息解析、继续流程辅助。
- `mikan.py`: Mikan 候选提取、补充 Bangumi 元数据、候选排序。
- `formatting.py`: 文本和卡片模板数据格式化。
- `utils.py`: 通用解析、类型转换、评分/季度格式化工具。

## 用户可见流程

- `add_mikan_subscription`: 搜索 Mikan，选择番剧和字幕组后添加。
- `recommend_mikan_subscription`: 推荐候选番剧，选择番剧和字幕组后添加。
- `search_mikan`: 搜索 Mikan 或查看字幕组 RSS。
- `add_subscription`: RSS 直连预览，确认后添加。
- `continue_pending`: 继续挂起任务。
- `list_subscriptions`: 查看 ANI-RSS 已启用订阅。
- `refresh_subscription`: 刷新单个订阅。
- `refresh_all`: 刷新全部订阅。

诊断流程：

- `check_status`: 检查 ANI-RSS API 连通性。
- `preview_subscription`: 只预览订阅结构，不写入。

## 挂起任务 ID

任务 ID 使用 `ani` + 8 位 hex，例如 `anif237a38d`。

用户可输入完整 ID，也可输入当前会话不冲突的前几位或后几位，但必须保留 `ani` 前缀：

```text
/anif237a38d 1
/anif237 1
/ania38d 1
```

不支持旧格式 `ani-xxxx`、裸短码 `68d2`、`继续anixxxx`。
