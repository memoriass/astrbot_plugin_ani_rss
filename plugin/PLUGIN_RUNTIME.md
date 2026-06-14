# Plugin Runtime

AstrBot 插件运行时支撑模块。由于 AstrBot loader 以根目录 `main.py` 作为插件类发现边界，`AniRssPlugin` 类保留在 `main.py`；具体运行职责在本目录拆分。

## 文件

- `settings.py`: 插件配置读取、ANI-RSS client、wake prefix、渲染配置。
- `pending_store.py`: 挂起任务创建、过期清理、短 ID 匹配。
- `subscription_builder.py`: RSS 类型识别、Mikan bgmUrl 补齐、订阅结构构造。
- `tooling.py`: 工具调用事件提取、自然语言提示修正、帮助文本。
