# mycode - 编程智能体

> ⚠️ **警告**：本项目正在开发中，不建议在非测试环境使用。

## 功能特性

- **智能对话**：基于 OpenAI 兼容 API 的交互式编程助手
- **工具调用**：支持自定义工具注册系统（`ToolsRegistry`）
- **命令执行**：集成 bash 工具，直接执行 shell 命令
- **会话管理**：完整的对话上下文管理，支持多轮交互与断点续接
- **命令历史**：持久化保存输入历史，支持上下键翻阅
- **自动补全**：内置命令补全功能

## 项目结构

```
myc/
├── pyproject.toml          # uv 项目配置（依赖、入口、构建）
├── uv.lock                 # 依赖锁文件（由 uv 自动生成）
├── .python-version         # Python 版本（uv 自动读取）
├── .env.example            # 环境变量模板
├── src/mycode/             # 主包
│   ├── __init__.py
│   ├── __main__.py         # 支持 `python -m mycode`
│   ├── cli.py              # CLI 入口逻辑
│   ├── session.py          # 会话管理与 ADT 事件类型
│   ├── registry.py         # 工具注册表（ToolsRegistry）
│   ├── tools/
│   │   ├── __init__.py     # 导入以触发工具注册
│   │   └── bash.py         # bash 工具
│   └── py.typed            # PEP 561 类型标记
├── tests/                  # 测试（pytest）
│   ├── test_session.py
│   ├── test_tools.py
│   └── test_registry.py
├── docs/dev/event_design.md         # 事件架构设计文档
├── LICENSE
└── README.md
```

## 快速开始

### 前置要求

- [uv](https://docs.astral.sh/uv/)（推荐，>= 0.4）
- Python 3.10+（uv 会根据 `.python-version` 自动管理）
- OpenAI API 密钥（或兼容的 API 服务）

### 安装与同步依赖

```bash
uv sync
```

`uv sync` 会自动创建 `.venv/` 虚拟环境并安装所有依赖（含 dev 依赖）。

### 配置环境变量

复制模板并填写真实值：

```bash
cp .env.example .env
# 编辑 .env 填入 API_KEY 等
```

`.env` 文件位于 git 忽略列表，请勿提交。

### 运行

任选其一：

```bash
# 通过 uv 管理的脚本入口（myc / mycode 均可）
uv run myc

# 等价于
.venv/bin/myc

# 别名入口
uv run mycode

# 也可以作为模块调用
uv run python -m mycode
```

### 续接上次的会话

```bash
uv run myc -r <session_uuid>
```

## 使用方法

启动后会进入交互式命令行界面，直接输入你的需求即可。例如：

- `创建一个 Python 项目结构`
- `在当前目录列出所有文件`
- `帮我写一个 Flask Web 应用`

## 开发

### 运行测试

```bash
uv run pytest
```

### 类型检查

```bash
uv run mypy src
```

### 添加新工具

1. 在 `src/mycode/tools/` 下新建模块，例如 `mycode/tools/echo.py`：

   ```python
   from typing import Annotated
   from mycode.registry import ToolsRegistry

   @ToolsRegistry.tool(description="回显文本")
   def echo(text: Annotated[str, "要回显的内容"]) -> str:
       return text
   ```

2. 在 `src/mycode/tools/__init__.py` 中导入该模块以触发装饰器注册：

   ```python
   from mycode.tools import bash, echo  # noqa: F401
   ```

3. 编写测试到 `tests/`，运行 `uv run pytest`。

## License

MIT License. 详见 [LICENSE](./LICENSE) 文件。
