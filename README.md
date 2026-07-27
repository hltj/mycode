# mycode - 编程智能体

> ⚠️ **警告**：本项目正在开发中，不建议在非测试环境使用。

## 功能特性

- **智能对话**：基于 OpenAI 兼容 API 的交互式编程助手
- **工具调用**：支持自定义工具注册系统（`ToolsRegistry`）
- **命令执行**：内置 `bash` 工具，直接执行 shell 命令
- **文件检索**：内置 `ls`、`glob`、`grep`，按行号/KiB 截断输出
- **文件读写**：内置 `read`、`write`、`edit`、`patch`，统一走路径安全检查
- **任务跟踪**：内置 `todo_write`，搭配陈旧度自动提醒与重放同步
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
│   ├── tools_registry.py   # 工具注册表（ToolsRegistry）
│   ├── tools/
│   │   ├── __init__.py     # 导入以触发工具注册
│   │   ├── _safe_path.py    # 路径安全检查（CWD 内 + 保护正则）
│   │   ├── _truncate.py     # KiB 输出截断
│   │   ├── bash.py          # bash 命令执行
│   │   ├── ls.py            # ls -la
│   │   ├── glob.py          # fd / find glob
│   │   ├── grep.py          # rg / grep 搜索
│   │   ├── read.py          # 带行号读文件
│   │   ├── write.py         # 写文件
│   │   ├── edit.py          # 字符串替换编辑
│   │   ├── patch.py         # 应用 unified diff
│   │   └── todo_write.py    # 内存 TODO 列表
│   └── py.typed            # PEP 561 类型标记
├── tests/                  # 测试（pytest）
│   ├── test_session.py
│   ├── test_tools.py
│   ├── test_tools_registry.py
│   ├── test_safe_path.py
│   └── test_truncate.py
├── docs/dev/event_design.md         # 事件架构设计文档
├── LICENSE
└── README.md
```

## 快速开始

### 前置要求

- [uv](https://docs.astral.sh/uv/)（推荐，>= 0.4）
- Python 3.10+（uv 会根据 `.python-version` 自动管理）
- OpenAI API 密钥（或兼容的 API 服务）
- 可选外部命令：`fd` 或 `find`（用于 `glob`）、`rg` 或 `grep`（用于 `grep`）、
  `patch`（用于 `patch`）。`mycode` 会按 `fd → find`、`rg → grep` 顺序回退。

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

`.env.example` 中可配置的关键项：

| 变量                      | 说明                                                                    | 默认值                    |
| ------------------------- | ----------------------------------------------------------------------- | ------------------------- |
| `API_KEY`                 | OpenAI 兼容 API 的密钥                                                  | （必填）                  |
| `BASE_URL`                | OpenAI 兼容 API 的 Base URL                                             | OpenAI 官方                |
| `MODEL_NAME`              | 默认模型名                                                              | （必填）                  |
| `ADDITIONAL_SYSTEM_PROMPT` | 附加系统提示词，拼接在内置提示词之后（用换行分隔）；留空表示无追加      | （空）                    |
| `BASH_TIMEOUT`            | `bash` 工具的超时（秒）                                                 | `60`                      |
| `BASH_DANGEROUS`          | 逗号分隔的危险命令子串列表，命中即拒绝执行                              | `sudo,rm -rf`             |
| `MYCODE_HOME_DIR`         | mycode 的应用目录（存放会话与历史）                                     | `~/.mycode`               |
| `MYCODE_PROTECTED_PATH_PATTERN` | 逗号分隔的受保护路径正则；路径命中任一条则 `ls/glob/grep/read/write/edit/patch` 拒绝访问 | （空）                |
| `MYCODE_STALE_THRESHOLD`  | `todo_write` 陈旧度阈值（连续 N 轮未更新且有未完成项则注入提醒）         | `3`                       |

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
# 或
uv run myc -c   # 恢复当前目录的最新会话
```

## 内置工具一览

所有工具均在 `ToolsRegistry` 中注册，启动后即可被智能体调用。除 `bash` 外
的文件类工具均受 `MYCODE_PROTECTED_PATH_PATTERN` 保护，并使用统一的
行数 + KiB 联合截断：任一上限触发即在行内不被切断的前提下追加
`"\n... 已截断"` 标记。

| 工具          | 用途                                                                 |
| ------------- | -------------------------------------------------------------------- |
| `bash`        | 执行 shell 命令；按 `BASH_TIMEOUT` 超时，命中 `BASH_DANGEROUS` 拒绝   |
| `ls`          | 类似 `ls -laF`：权限、大小、ISO-8601 日期、类型后缀（`/` `*` `@` `|` `=`） |
| `glob`        | 按 glob 模式匹配路径（`fd --glob` 优先，回退 `find`）                |
| `grep`        | 在文件/目录中按正则/字面量搜索（`rg` 优先，回退 `grep`）             |
| `read`        | `cat -n` 风格读文件，支持 `offset`/`limit`/`truncate`                |
| `write`       | 覆盖写入文件，自动创建父目录                                         |
| `edit`        | 按 `old_text`/`new_text` 替换；`replace_all` 控制全部替换            |
| `patch`       | 应用 unified diff（自动检测 `-p0`/`-p1`，先 dry-run 再正式应用）      |
| `todo_write`  | 整体替换内存 TODO 列表；同时只能有一项 `in_process`                  |

> **路径安全**：所有文件类工具在处理前都会通过 `safe_path()`，拒绝超出
> CWD 的路径（含跟随软链接后越界）以及命中 `MYCODE_PROTECTED_PATH_PATTERN`
> 的路径。越界时返回 `Error: 路径 '...' 超出当前工作目录`。

### `todo_write` 与陈旧度提醒

- `todo_write(items)` 整体替换当前 TODO 列表；空列表表示清空。
- 每产生一个 assistant 消息时自增一次陈旧度计数；
  - 超过 `MYCODE_STALE_THRESHOLD` 且存在未完成项时，往 `messages` 注入一条
    `<reminder>` 文本（模型下次 API 调用可见），同时派发 `ReminderEvent`
    在终端以黄色高亮显示并写入会话历史；
  - 调用 `todo_write` 成功后陈旧度计数自动清零。
- 重放历史时，`replay_history` 会在派发 `todo_write` 的 `ToolCallEvent`
  之后把 `_todo_state` 同步成调用时刻的列表，让对应的 `ToolResultEvent`
  渲染能看到当时的进度。

## 使用方法

启动后会进入交互式命令行界面，直接输入你的需求即可。例如：

- `创建一个 Python 项目结构`
- `在当前目录列出所有文件`
- `帮我写一个 Flask Web 应用`
- `给 README.md 加一段工具介绍`
- `把当前进度用 todo_write 记一下`

## 开发

### 运行测试

```bash
uv run pytest
```

测试覆盖：

- 工具注册表（`test_tools_registry.py`）
- 各内置工具的注册、参数、基础与边界行为（`test_tools.py`）
- 路径安全检查（`test_safe_path.py`）
- 行数/KiB 联合截断（`test_truncate.py`）
- 会话历史与 ADT 序列化往返（`test_session.py`）
- CLI 渲染、`replay` 同步、陈旧提醒等集成行为（`test_cli.py`）

### 类型检查

```bash
uv run mypy src
```

### 添加新工具

1. 在 `src/mycode/tools/` 下新建模块，例如 `mycode/tools/echo.py`：

   ```python
   from typing import Annotated
   from mycode.tools_registry import ToolsRegistry

   @ToolsRegistry.tool(description="回显文本")
   def echo(text: Annotated[str, "要回显的内容"]) -> str:
       return text
   ```

   若工具需要读写文件，建议复用 `safe_path()` 做路径安全检查，并用
   `cap_lines()` 处理大输出。

2. 在 `src/mycode/tools/__init__.py` 中导入该模块以触发装饰器注册：

   ```python
   from mycode.tools import bash, echo  # noqa: F401
   ```

3. 编写测试到 `tests/`，运行 `uv run pytest`。

## License

MIT License. 详见 [LICENSE](./LICENSE) 文件。
