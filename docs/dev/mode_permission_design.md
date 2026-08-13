# 模式与权限系统设计

## 概述

mycode 提供一套 **模式 + 权限** 系统，将工具调用分类后按当前模式（询问 / 自动 / 全权）决定是否需要人工确认，并配合确认交互界面（同意 / 编辑 / 拒绝）完成人工介入。本系统分为两层：

- **逻辑层**（`mycode.mode`）：模式的枚举、切换、工具分类、决策矩阵（是否需要确认）。
- **交互层**（`mycode.confirm`）：确认菜单与多行编辑界面的独立控件，基于 prompt_toolkit 布局实现。

核心文件：
- `src/mycode/mode.py` — 模式（`Mode`）、分类（`ToolCategory`）、决策（`needs_confirmation`）
- `src/mycode/confirm.py` — 确认界面、编辑界面、结果格式化
- `src/mycode/cli.py` — 工具执行前的权限拦截（`_run_tool_with_permission`）
- `src/mycode/session.py` — 会话中模式字段的持久化（每条消息携带 `mode`）

---

## 一、模式（Mode）

### 枚举

```python
class Mode(str, Enum):
    ASK = "ask"    # 询问
    AUTO = "auto"  # 自动（默认）
    YOLO = "yolo"  # 全权
```

- 默认模式为 **自动**。
- `Mode.label` 返回中文名（询问 / 自动 / 全权），用于提示符与渲染。

### 模式状态持有者 ModeState

```python
class ModeState:
    def get(self) -> Mode
    def set(self, mode: Mode) -> Mode
    def cycle(self) -> Mode   # 自动 → 全权 → 询问 → 自动
```

- 模块级单例 `MODE_STATE` 被 `cli`、`renderer` 共享，避免循环 import。
- `cycle()` 用于 shift-tab 循环切换，顺序为 `[AUTO, YOLO, ASK]`。

### 模式切换方式

| 方式 | 说明 |
|------|------|
| `shift-tab` | 循环切换（自动 → 全权 → 询问 → 自动） |
| `/ask`、`/auto`、`/yolo` | 手动切换到指定模式 |

切换时 `cli._switch_mode()` 更新 `MODE_STATE` 并派发 `ModeChangeEvent` 事件（渲染 + 持久化）。

### 提示符

两种渲染风格下提示符会体现当前模式（颜色 + 标记）：

| 模式 | classic | default | 颜色 |
|------|---------|---------|------|
| 询问 | `myc[询问] >` | `│? ` | 蓝 |
| 自动 | `myc[自动] >` | `│ ` | 绿 |
| 全权 | `myc[全权] >` | `│! ` | 橙 |

渲染器通过 `_MODE_PROMPT_STYLES`（模式 → 样式类）与 `_DEFAULT_PROMPT_PREFIXES`（default 前缀）查表生成。

---

## 二、操作分类（ToolCategory）

工具调用被分为六类：

```python
class ToolCategory(str, Enum):
    DANGEROUS = "dangerous"  # 危险
    CAUTION   = "caution"    # 注意
    UNKNOWN   = "unknown"    # 未知
    WRITE     = "write"      # 写
    READ      = "read"       # 读
    INTERNAL  = "internal"   # 内部
```

| 类别 | 判定 | 工具 |
|------|------|------|
| 危险 | bash 且命中 `BASH_DANGEROUS` 正则 | bash |
| 注意 | bash 且命中 `BASH_CAUTION` 正则 | bash |
| 未知 | bash 且未命中以上两类 | bash |
| 写 | 工具名匹配 | write / edit / patch |
| 读 | 工具名匹配 | ls / glob / grep / read |
| 内部 | 工具名匹配 | todo_write |

### 分类实现

```python
def classify_tool(func_name: str, args: dict | None) -> ToolCategory:
```

- `bash`：取 `args["command"]` 字符串，先匹配 `BASH_DANGEROUS`，命中即危险；再匹配 `BASH_CAUTION`，命中即注意；否则未知。
- 写 / 读 / 内部：按工具名集合查表。
- 其它未知工具名：归为 `UNKNOWN`。

环境变量正则用逗号分隔，`re.search` 匹配。`is_bash_tool()` 判断某类别是否源自 bash（危险 / 注意 / 未知），用于决定确认界面是否提供【编辑】。

---

## 三、决策矩阵（needs_confirmation）

```python
def needs_confirmation(mode: Mode, category: ToolCategory) -> bool:
```

| 类别 | 询问 | 自动 | 全权 |
|------|------|------|------|
| 危险 | 拒绝 | 拒绝 | 拒绝 |
| 注意 | 确认 | 确认 | 直接执行 |
| 未知 | 确认 | 直接执行 | 直接执行 |
| 写 | 确认 | 直接执行 | 直接执行 |
| 读 | 直接执行 | 直接执行 | 直接执行 |
| 内部 | 直接执行 | 直接执行 | 直接执行 |

> **危险**操作不进入此判定：所有模式下一律拒绝（由 `_run_tool_with_permission` 在调用 `needs_confirmation` 之前拦截）。

---

## 四、权限拦截与执行（cli）

`cli._run_tool_with_permission(func_name, args, handler)` 在工具执行前做权限检查：

```python
category = classify_tool(func_name, args)

# 所有模式对【危险】操作一律拒绝
if category == ToolCategory.DANGEROUS:
    return "Error: 拒绝执行危险命令"

# 无需确认：直接执行
if not needs_confirmation(MODE_STATE.get(), category):
    return handler(**args)

# 需确认：弹出确认界面
action, extra = confirm_tool(func_name, category, command)
```

确认结果的后续处理：

| 动作 | 处理 |
|------|------|
| `REJECT_NO_REASON` | 抛出 `_AbortLoop(format_reject_no_reason())` 跳出 agent 循环 |
| `CANCEL` | 抛出 `_AbortLoop(format_cancel())` 跳出 agent 循环 |
| `REJECT` | 返回 `format_reject(reason)` 拒绝文本 |
| `EDIT` | 编辑命令后**重新分类**，若变为危险则拒绝，否则执行 |
| `APPROVE` | 执行 handler |

`_AbortLoop` 是内部信号异常：取消 / 无理由拒绝时被 `agent_loop` 捕获，写入对应 tool 结果文本并分发 `InterruptEvent(abort=True)`（replay 时不渲染 `^C`）。

---

## 五、确认界面（confirm）

### 布局结构

确认界面与多行编辑界面在**同一个 prompt_toolkit `Application`** 内切换视图（`state.editing` 标记），避免非全屏模式下多次 `app.run()` 累积渲染旧菜单：

- **确认视图**（`_build_confirm_layout`）：逐行展示选项（同意 / 编辑 >> / 拒绝），选中“拒绝”时，理由输入框与拒绝行**同行**（`VSplit` 并列、紧挨一个空格）。
- **编辑视图**（`_build_edit_layout`）：“编辑 >> ” 提示 + 多行输入框，高度随内容行数自适应。进入编辑时确认菜单被**替换**（隐藏），返回时恢复。

### 选项模型

确认菜单的选项用结构化对象表示，每个选项自带类型，处理逻辑按类型分发（而非为某个选项特化）：

```python
class OptionKind(str, Enum):
    APPROVE = "approve"  # 同意
    EDIT    = "edit"     # 编辑
    REJECT  = "reject"   # 拒绝

class Option:
    kind: OptionKind   # 选项类型
    label: str         # 显示文本（不含序号）
```

- `_ConfirmState.options` 返回 `list[Option]`（同意 + [编辑 >>] + 拒绝），序号在渲染时用 `enumerate` 统一附加。
- 拒绝是选项列表的最后一项，通过 `options[sel].is_reject` 判断，无特化命名。
- 未选中拒绝时，键盘输入被丢弃（不进入理由缓冲区），避免焦点残留污染理由。

### 应用状态机

单次 `app.run()` 内完成全部交互，视图切换在 key binding 内部通过 `_rebuild()` 更新 `app.layout.container`：

| 上下文 | 按键 | 行为 |
|--------|------|------|
| 确认菜单 | `↑`/`↓`（或 `c-p`/`c-n`） | 移动选择 |
| 确认菜单 | `Enter` | 确认当前项（同意 / 拒绝 / 进入编辑） |
| 编辑视图 | `Enter` | 插入换行（多行编辑） |
| 编辑视图 | `Alt+Enter`（`ESC`随后`Enter`） | 提交编辑 |
| 编辑视图 | `ESC` | 返回确认菜单 |
| 任意 | `Ctrl-C` | 中止（`abort`） |

### 动作与返回

`_run_confirm_menu` 返回 `(action, text)`：

| action | 含义 |
|--------|------|
| `approve` | 同意执行 |
| `reject` | 拒绝（text 为理由） |
| `reject_plain` | 无理由拒绝 |
| `edit` | 编辑（text 为编辑后的命令） |
| `abort` | Ctrl-C 中止 |

`confirm_tool` 将动作映射为 `ConfirmAction` 枚举（`APPROVE` / `REJECT` / `REJECT_NO_REASON` / `EDIT` / `CANCEL`）返回给调用方。确认交互界面是独立模块，可通过注入 `input` / `output` 在测试中驱动按键序列。

### 编辑器能力

编辑缓冲区为 `Buffer(multiline=True)`，支持：
- 多行命令（如 10 行 here-doc）完整编辑
- 光标在任意行的开头 / 中间 / 末尾移动并修改（`c-a` 行首、`c-e` 行尾、`c-p`/`c-n` 上下行、`c-f` 右移）
- 多行输入用 `Alt+Enter` 提交，`Enter` 换行

### 结果格式化

| 函数 | 输出 |
|------|------|
| `format_reject(reason)` | `Error: 用户拒绝执行：{reason}` |
| `format_reject_no_reason()` | `Error: 用户拒绝执行，未提供理由` |
| `format_cancel()` | `Error: 用户取消操作` |

---

## 六、会话与模式持久化

每条 `AgentMessage` 都携带 `mode` 字段（`MessageProtocol` 共享），在 `dispatch` 时由 `SessionHistory.inject_meta()` 注入当时的 `MODE_STATE`：

```python
def inject_meta(self, msg) -> None:
    msg.id = self._next_id()
    msg.parent_id = self.entries[-1].id if self.entries else None
    msg.time = get_iso_timestamp()
    msg.mode = MODE_STATE.get().value
```

这样：
- 用户消息渲染直接用 `msg.mode`（回放时按消息当时模式显示提示符，与 `MODE_STATE` 无关）。
- 会话恢复时从**最后一条消息**的 `mode` 恢复当前模式（`SessionHistory.load`），回到退出前的模式。

模式切换本身作为 `ModeChangeEvent` 事件持久化（`entry_type="mode_change"`），记录切换时机。

---

## 七、开发规范

1. **新增工具类别**：在 `ToolCategory` 添加枚举值，更新 `classify_tool` 与 `needs_confirmation` 决策矩阵。
2. **调整确认规则**：只改 `needs_confirmation` 分支。
3. **扩展确认动作**：在 `ConfirmAction`、`OptionKind` 与 `_run_confirm_menu` 的 action 集合中同步增加，并在 `confirm_tool` 的映射处补全。
4. **新增/调整确认选项**：修改 `_ConfirmState.options`（返回 `list[Option]`），序号由渲染统一附加，处理逻辑按 `OptionKind` 分发，勿为单个选项特化命名。
5. **新增渲染模式标记**：在 `mode.MODE_COLOR`、`renderer._MODE_PROMPT_STYLES` / `_DEFAULT_PROMPT_PREFIXES` 同步更新。
6. **测试**：优先以 TDD 方式为决策 / 交互 / 布局写测试（见 `tests/test_confirm.py`、`tests/test_mode.py`）。
