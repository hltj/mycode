# CLI 渲染设计

## 概述

CLI 的终端展示由 `mycode.renderer`（渲染器）与 `mycode.cli`（prompt_toolkit 输入区、事件总线装配）共同完成。渲染按风格（`--style`，默认 `default`，可选 `classic`）拆分为独立渲染器：公共流程在 `_Renderer` 基类复用，风格差异由 `_DefaultRenderer` / `_ClassicRenderer` 子类覆写。所有消息 / 事件统一走 `AgentEventBus` 分发到 `render_terminal`（实时）或 `render_replay`（历史重放）。

核心文件：
- `src/mycode/renderer.py` — 渲染器基类与两个风格子类、共享分发、TODO 渲染、ANSI 辅助
- `src/mycode/cli.py` — 事件总线、PromptSession 配置、输入区样式、replay 装配

---

## 一、渲染风格（--style）

`main()` 从命令行参数 `-s/--style` 读取风格，调用 `set_render_style(style)` 设置模块级 `RENDER_STYLE`，`_get_renderer()` 按需惰性实例化并缓存到 `_RENDERERS`。

| 风格 | 类 | AI 标题 | 工具调用标题 | 工具输出标题 | 待办符号 | 提示符 |
|------|----|---------|--------------|--------------|----------|--------|
| `default` | `_DefaultRenderer` | `🤖 {model}` | `🔧 调用工具 - x` | `📤 工具输出` | emoji（🔳/🟧/✅️） | `│` / `│?` / `│!` |
| `classic` | `_ClassicRenderer` | `AI【{model}】` | `调用工具 - x` | `工具输出` | 复选框（`- [ ]:` 等） | `myc[模式] >` |

模块级 `RENDER_STYLE` 是全局状态；测试通过 `monkeypatch.setattr(renderer, "RENDER_STYLE", ...)` 切换。

---

## 二、渲染器架构（_Renderer 抽象）

### 公共流程（基类实现，复用给所有风格）

| 方法 | 说明 |
|------|------|
| `format_todos(state)` | 用子类 `symbols` + 共用 `formats` 模板渲染待办列表 |
| `render_assistant(message, model)` | AI 标题（紫）+ 正文；无正文（纯 tool_calls）仅标题 |
| `render_tool_call(tool_call)` | 工具名标题（蓝）+ YAML 参数（代码围栏） |
| `render_tool_result(message, tool_name)` | 工具输出标题（蓝）；`todo_write` 特化：先输出 TODO 列表再输出结果 |
| `render_reminder(content)` | 黄色高亮提醒 |
| `render_exception(exc)` | 红色异常标题 + traceback 围栏 |
| `render_interrupt()` | 输出空行 |
| `render_mode_change(mode)` | 灰色提示「已切换到【{mode}】模式」 |

### 风格差异点（子类覆写）

| 方法 | 说明 |
|------|------|
| `ai_title` / `tool_call_title` / `tool_result_title` | 标题文本（emoji 与否） |
| `reminder_text` / `exception_title` | 提醒 / 异常标题文本 |
| `render_user_message(text, mode)` | 用户消息渲染（default 灰色背景块 / classic 单行前缀） |
| `prompt_prefix(mode)` | 提示符文本（不含尾随空格） |
| `create_prompt_style()` | 提示符样式表（Style） |
| `apply_input_style(session)` | 输入区背景附加样式（default 特有） |

### 共享分发（_render_common）

```python
def _render_common(msg: AgentMessage) -> None:
    renderer = _get_renderer()
    match msg:
        case SessionRecord(): pass                       # 不渲染
        case UserMessage(message, mode): renderer.render_user_message(...)
        case AssistantMessage(message, model): renderer.render_assistant(...)
        case ToolCallEvent(tool_call): renderer.render_tool_call(...)
        case ToolResultEvent(message, tool_name): renderer.render_tool_result(...)
        case InterruptEvent(): renderer.render_interrupt()
        case ReminderEvent(content): renderer.render_reminder(content)
        case ModeChangeEvent(mode): renderer.render_mode_change(mode)
        case ExceptionEvent(exception): renderer.render_exception(exception)
        case _ as unreachable: assert_never(unreachable)
```

`match-case` 顺序与 `AgentMessage` 联合类型一致，末尾 `assert_never` 保证 exhaustiveness。

### render_terminal / render_replay

```python
def render_terminal(msg):  # 实时交互：全部委托 _render_common
    _render_common(msg)

def render_replay(msg):    # 历史重放
    match msg:
        case InterruptEvent(abort=False):
            print("^C"); print()   # 真实 Ctrl-C：模拟 ^C
        case _:
            _render_common(msg)
```

`InterruptEvent` 按 `abort` 标记区分：真实 Ctrl-C（`abort=False`）重放时输出 `^C`；
确认界面取消 / 无理由拒绝（`abort=True`）不输出 `^C`。

---

## 三、渲染细节

### 3.1 颜色

ANSI 转义常量集中在 renderer 顶部，统一由 `mycode.mode.MODE_COLOR` 提供模式色：

| 用途 | 颜色 |
|------|------|
| AI 标题 | 紫 `\x1B[35m` |
| 工具调用 / 输出标题 | 蓝 `\x1B[1;34m` |
| 提醒 | 黄 `\x1B[1;33m` |
| 异常 | 红 `\x1B[1;31m` |
| 模式切换提示 | 灰 `\x1B[90m` |
| default 输入区背景 | `\x1B[48;2;51;51;51m`（RGB 灰） |
| 模式色（提示符/用户消息） | `MODE_COLOR`：询问蓝 / 自动绿 / 全权橙 |

### 3.2 代码围栏（_code_fence）

根据内容中最长连续反引号长度动态选择定界符：内容含 3 重反引号则用 4 重，以此类推
（`最长连续反引号 + 1`，最短 3 重），避免内容中的反引号提前终止代码块。

### 3.3 YAML 参数展示（render_tool_call）

工具调用参数用 YAML 展示而非 JSON：

1. `json.loads(arguments)` 解析参数；
2. 自定义 `_BlockStrDumper`：含换行的字符串用 block literal（`|-`）输出，避免换行折叠翻倍；
3. `yaml.dump(allow_unicode=True, sort_keys=False, default_flow_style=False)`；
4. 解析失败（非法 JSON）时直接原样输出参数字符串。

### 3.4 TODO 渲染（format_todos）

模板共用（`_TODO_FORMATS`），符号由子类提供：

| 状态 | default 符号 | classic 符号 | 标题样式 |
|------|--------------|--------------|----------|
| completed | `✅️` | `- [{x}]:`（x 绿） | 灰 + 删除线 |
| in_process | `🟧` | `- [{>}]:`（> 橙） | 粗 + 白 |
| pending | `🔳` | `- [ ]:` | 普通 |

### 3.5 default 用户消息（灰色背景块）

`_DefaultRenderer.render_user_message` 是 default 风格最复杂的部分：

- 首行带提示符（`│` / `│?` / `│!`，用当前模式色），续行顶格无前缀（与输入区
  `prompt_continuation=''` 一致）；
- 按显示宽度自行分行（`_wrap_by_display_width`），每段**填充到终端宽度**，
  保证背景从消息第一行到最后一行全量覆盖——不依赖终端自动换行（自动换行后
  短段没有背景色，且宽字符在行尾的截断行为各终端不一致）；
- 宽字符（中文占 2 列）在行尾放不下时当前行补空格、字符转下行；
- 零宽字符（emoji 修饰符）跟随前一字符，不计宽度；
- 提示符竖线用模式色渲染，空格 + 正文恢复默认前景（`_FG_DEFAULT`），保留背景色。

### 3.6 classic 用户消息

单行：`{模式色}{myc[模式] >} \x1B[0m{text}`，末尾空行。

---

## 四、输入区（prompt_toolkit）

`cli._create_prompt_session()` 创建 `PromptSession`：

1. `multiline=True` + `prompt_continuation=''`（续行无前缀）；
2. `completer=MycCommandCompleter()`：`/` 开头补全 `/q /quit /ask /auto /yolo`；
3. `style=renderer.create_prompt_style()`：模式 → 样式类
   （`mycode-prompt` 绿 / `mycode-prompt-ask` 蓝 / `mycode-prompt-yolo` 橙）；
4. `key_bindings`：`shift-tab` 绑定模式循环切换，以 `__mode_cycle__` 退出 prompt，
   由 `main` 下一轮读取后派发 `ModeChangeEvent`；
5. `erase_when_done=True`：下一轮渲染前擦除上一行输入。

提示符片段：

```python
def prompt_fragments(self) -> list[tuple[str, str]]:
    mode = MODE_STATE.get()
    return [(f"class:{_MODE_PROMPT_STYLES[mode]}", self.prompt_prefix(mode) + " ")]
```

- default：`│ `（自动）/ `│? `（询问）/ `│! `（全权）
- classic：`myc[自动] >` / `myc[询问] >` / `myc[全权] >`

### default 输入区灰色背景（apply_input_style）

style 表中 `''` 根样式 + `mycode-input` 类两条配合：

- `''` 根样式：让**有内容**的单元格继承灰色背景；
- `apply_input_style` 把 `class:mycode-input` 挂到布局根容器（HSplit）：
  parent_style 会下发到所有子窗口及「剩余空间」占位窗口（`_Split.write_to_screen`
  → `Window._apply_style` → `Screen.fill_area`），把整块输入区（含空行、行尾空白、
  以及向下延伸到终端底部的剩余空间）都填充为灰色。单靠根样式只会给已渲染字符上色，
  空白行不会被覆盖。

---

## 五、事件总线与渲染装配（cli）

```python
bus = AgentEventBus(session_hist=session_hist)
bus.register(make_persist_handler(session_hist))  # 先持久化
bus.register(render_terminal)                     # 再渲染
```

- `dispatch` 先由 `session_hist.inject_meta` 注入 id/parent_id/time/mode，再按注册顺序通知各 handler；
- 实时交互：`render_terminal` 委托 `_render_common`；
- 历史重放：`replay_history` 建**不带 session_hist** 的总线，注册 `render_replay` 与
  `make_replay_todo_sync_handler()`（把 `todo_write` 的 `ToolCallEvent` 实时同步到
  `_todo_state`，让对应 `ToolResultEvent` 渲染能看到调用时刻的待办列表）。

**渲染顺序**（`agent_loop`）：

1. `bus.dispatch(ReminderEvent)`（陈旧待办提醒，如有）
2. `bus.dispatch(AssistantMessage)` → AI 标题 + 正文
3. 每个 pending tool_call：`bus.dispatch(ToolCallEvent)` → 渲染「🔧 调用工具」
   与 YAML 参数；执行后 `bus.dispatch(ToolResultEvent)` → 渲染「📤 工具输出」
   （todo_write 先渲染 TODO 列表）
4. 中断 / 异常：`bus.dispatch(InterruptEvent)` / `ExceptionEvent`

---

## 六、开发规范

1. **新增事件渲染**：在 `_Renderer` 基类加公共方法（或标记为子类差异点），在
   `_render_common` 的 match 里按联合类型顺序补 `case`，末尾保留 `assert_never`。
2. **风格差异**：优先在子类覆写标题 / 提示符 / 用户消息方法，公共流程留基类。
3. **颜色**：复用 renderer 顶部 ANSI 常量与 `MODE_COLOR`，不要在各处硬编码转义码。
4. **新增模式标记**：在 `mode.MODE_COLOR`、`renderer._MODE_PROMPT_STYLES` /
   `_DEFAULT_PROMPT_PREFIXES` 同步更新。
5. **测试**：见 `tests/test_renderer.py`（标题、emoji、TODO 符号、代码围栏、
   显示宽度分行、灰色背景填充、提示符片段等）。
