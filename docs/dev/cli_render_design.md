# CLI 渲染设计

## 概述

CLI 的终端展示由 `mycode.renderer`（渲染器）与 `mycode.cli`（prompt_toolkit 输入区、事件总线装配）共同完成。渲染按风格（`--style`，默认 `default`，可选 `classic`）拆分为独立渲染器：公共流程在 `_Renderer` 基类复用，风格差异由 `_DefaultRenderer` / `_ClassicRenderer` 子类覆写。所有消息 / 事件统一走 `AgentEventBus` 分发到 `render_terminal`（实时）或 `render_replay`（历史重放）。

default 风格引入 `rich` 实现代码块的语法高亮与 assistant 正文的
Markdown 渲染；classic 风格不引入，保持纯代码围栏与原样输出。

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
| `render_assistant(message, model)` | AI 标题（紫）+ 正文（正文经 `render_assistant_body`，default 为 rich Markdown）；无正文（纯 tool_calls）仅标题 |
| `render_assistant_body(body)` | 渲染 assistant 正文：default 用 rich Markdown / classic 原样输出 |
| `render_tool_call(tool_call)` | 工具名标题（蓝）+ YAML 参数（default 语法高亮 / classic 围栏） |
| `render_tool_result(tool_result)` | 工具输出标题（蓝）；`todo_write` 特化：先输出 TODO 列表再输出结果；`read` 特化：带行号渲染 |
| `render_notice(notice)` | 黄色高亮提醒（仅标题用提醒格式，附加内容 default 解析围栏后语法高亮 / classic 照常输出） |
| `render_exception(exc)` | 红色异常标题 + traceback（default 语法高亮 / classic 围栏） |
| `render_interrupt()` | 输出空行 |
| `render_mode_change(mode)` | 灰色提示「已切换到【{mode}】模式」 |

### 代码块渲染（基类差异点）

| 方法 | 说明 |
|------|------|
| `render_code_block(body, language)` | 无行号代码/文本（default：rich 语法高亮 / classic：代码围栏） |
| `render_tool_call_params(func_name, params, yaml_text, call_id)` | 工具调用参数渲染（default 对 bash/write/patch/edit 特化，见 3.5.1；classic 沿用 YAML 参数块） |
| `render_read_output(content)` | read 工具返回（default：rich 带行号语法高亮 / classic：围栏） |
| `render_notice_additional(additional)` | 提醒附加内容（default：解析围栏后高亮 / classic：原样） |

### 风格差异点（子类覆写）

| 方法 | 说明 |
|------|------|
| `render_code_block` / `render_tool_call_params` / `render_read_output` / `render_notice_additional` | 代码块 / 工具调用参数 / read 返回 / 提醒附加内容渲染（default 语法高亮与特化 / classic 围栏） |
| `render_assistant_body` | assistant 正文渲染（default rich Markdown / classic 原样） |
| `ai_title` / `tool_call_title` / `tool_result_title` | 标题文本（emoji 与否） |
| `notice_text` / `exception_title` | 提醒 / 异常标题文本 |
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
        case ToolResultEvent(tool_result): renderer.render_tool_result(tool_result)
        case InterruptEvent(): renderer.render_interrupt()
        case NoticeEvent(notice): renderer.render_notice(notice)
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
        case InterruptEvent(interrupt={"abort": False}):
            print("^C"); print()   # 真实 Ctrl-C：模拟 ^C
        case ToolCallEvent(tool_call):
            renderer.render_tool_call(tool_call, replay=True)  # edit 不读文件
        case _:
            _render_common(msg)
```

`InterruptEvent.interrupt["abort"]` 标记区分：真实 Ctrl-C（`abort=False`）
重放时输出 `^C`；确认界面取消 / 无理由拒绝（`abort=True`）不输出 `^C`。
`ToolCallEvent` 单独分支（见 3.5.1）：replay 时文件可能已不存在/已改变，
edit 不读实际文件、用片段级 diff 并把 `@@` 行号替换为 `@@ ... @@`；其余
工具类型照常委托 `_render_common`。

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

classic 风格下按内容中最长连续反引号长度动态选择定界符：内容含 3 重反引号则用
4 重，以此类推（`最长连续反引号 + 1`，最短 3 重），避免内容中的反引号提前终止
代码块。

### 3.3 rich 语法高亮（default，_syntax_plain）

default 风格用 `rich.syntax.Syntax` 渲染代码块，替代代码围栏：

- 无行号内容（工具调用 YAML、工具结果、异常 traceback）：`Syntax(..., line_numbers=False)`
  直接语法高亮输出；
- 语言标签传递：工具调用参数填 `yaml`，异常 traceback 用 `python`（`traceback_language`，
  对 File/行号/异常名/关键字均有高亮）。**工具结果语言猜测仅限 `read` / `bash`**：
  - `bash` 输出经 Pygments `guess_lexer` 按内容猜测（如输出 Python 代码）；
  - `read` 按调用时 `file_path` 推断（见下文 3.6）；
  - 其余工具（`write` / `edit` / `glob` / `grep` / `ls` / `patch` 等）写死
    `text`（纯文本、不做语法猜测）。
  当语言参数为 `None` / 非法时，`_syntax_plain` 回退 `markdown` 词法分析；
  写死的 `text` 是显式语言，直接按纯文本渲染、不参与该兜底；
- 背景色：代码块区域设置 `background_color="rgb(30,30,30)"`，与输入区灰色背景呼应、
  无围栏时仍能区分代码块边界；
- 主题：语法高亮默认使用 `nord`（低饱和蓝灰，柔和不刺眼），可通过环境变量
  `MYCODE_SYNTAX_THEME` 覆盖（如 `gruvbox-dark` / `zenburn` / `one-dark`），
  常量集中在 renderer 顶部（`_CODE_THEME` / `_CODE_BG`）；
- ANSI 控制码豁免：工具输出若自带 ANSI 控制码（如 `ls --color`、脚本彩色输出），
  不做语法高亮、原样输出（`_has_ansi_control` 检测），避免控制码被语法上色导致
  转义序列二次包装、颜色错乱；
- 宽度：按当前终端宽度渲染（`shutil.get_terminal_size`，获取失败回退 80），配合
  `Syntax(word_wrap=True)` 让超长行在终端内自动换行同时保持背景色铺满整行；
  `Console(file=io.StringIO(), force_terminal=True)` 捕获 ANSI 转义后经 `print` 输出。

工具调用参数会登记到模块级缓存 `_TOOL_CALL_INFO`（`tool_call_id` → 参数 dict），
供结果渲染时按 id 取回以推断语言（read 的文件路径等）。

### 3.4 default 风格 assistant 正文 rich Markdown 渲染（_markdown_plain）

`_markdown_plain(markup)`（`renderer.py`）把 assistant 正文字体交给
`rich.markdown.Markdown` 渲染，替代原来的纯文本输出：

- 段落 / 标题 / 列表 / 表格 / 引用 / 分割线 / 内联样式（**加粗**、*斜体*、
  `code`、~~删除线~~）等按 rich 默认主题渲染（富文本着色）；
- 代码块（`fence` / `code_block`）用 **覆写版 `CodeBlock` 子类**：与工具输出
  一致取 `_CODE_BG` 背景色与 `_CODE_THEME` 主题（`Syntax(word_wrap=True,
  padding=1, background_color=...)`），避免富文本对 ``` 围栏做默认的
  cyan-on-black 内联处理、与工具输出视觉割裂；实例级覆写
  `Markdown.elements` 映射（不影响 rich 全局 `Markdown.elements`）。
  代码块语言标签非法时由 `Syntax` 抛出异常，子类捕获后回退 `text`；
- ANSI 控制码豁免：正文自带 ANSI 转义（如模型回显控制符）时原样输出、不经过
  markdown 解析，避免转义序列被再次装箱导致泄漏（`_has_ansi_control`）；
- 宽度：`Console(force_terminal=True, width=_terminal_columns())` 按终端宽度
  渲染；极端的输入导致 rich 解析/渲染异常时兜底为纯文本输出。

注意：`render_assistant` 的公共流程在基类（标题 + 正文 + 尾部空行），
正文渲染委托给 `render_assistant_body`；default 覆写为 `_markdown_plain`，
classic 沿用基类的原样输出（不引入 rich 解析，与代码围栏策略一致）。

### 3.5 YAML 参数展示（render_tool_call）

工具调用参数用 YAML 展示而非 JSON：

1. `json.loads(arguments)` 解析参数；
2. 自定义 `_BlockStrDumper`：含换行的字符串用 block literal（`|-`）输出，避免换行折叠翻倍；
3. `yaml.dump(allow_unicode=True, sort_keys=False, default_flow_style=False)`，
   并去掉末尾换行（YAML 块内不出现多余的背景空行）；
4. 解析失败（非法 JSON）时直接原样输出参数字符串。

default 去掉原来的 ```yaml 代码围栏，改用 rich 语法高亮渲染（`language="yaml"`）；
classic 保持 ```yaml 围栏。

#### 3.5.1 工具调用参数特化渲染（default 风格，bash/write/patch/edit）

default 风格下，`render_tool_call_params` 对 `bash` / `write` / `patch` /
`edit` 四个工具做特化处理，其 YAML 参数块去掉大字段并追加新代码块展示
（classic 风格不做特化、保持完整 YAML 参数围栏）：

| 工具 | YAML 去掉的字段 | 新代码块语法 | 行号 | 展示内容 |
|------|-----------------|--------------|------|----------|
| `bash` | `command` | `bash` | 带 | 命令文本 |
| `write` | `content` | 按 `file_path` 推断（Pygments） | 带 | content |
| `patch` | `diff` | `diff` | 不带 | diff |
| `edit` | `old_text` / `new_text` | `diff` | 不带 | 二者的 unified diff（`difflib.unified_diff`，去 `---`/`+++` 文件头，`@@`/`+`/`-` 着色） |

具体规则：

- **bash**：原 YAML 不再展示 `command`；命令文本紧邻标题（中间无空行），
  用 `bash` 语法、**带行号** `Syntax(command, "bash", line_numbers=True)`
  渲染命令文本（rich 行号连续自然排列）；
- **write**：原 YAML 只保留 `file_path`（去掉 `content`）；添加一个空行后，
  新代码块按 `file_path` 用 `guess_lexer_for_filename` 推断语言（扩展名如
  `.py`/`.sh`/`.yaml`，已知文件名如 `.bashrc` 直接命中），**带行号**展示
  content；文件名推断不到时按内容 `guess_lexer`，纯文本由 `_syntax_plain`
  回退 `markdown`；
- **patch**：原 YAML 只保留 `dir_path`（去掉 `diff`）；添加一个空行后，
  新代码块用 `diff` 语法、**不带行号**展示 diff（`---`/`+++`/`@@`/`+`/`-`
  及 diff 头均按 diff 词法上色）；
- **edit**：原 YAML 去掉 `old_text`/`new_text`（保留 `file_path` 与
  `replace_all`）；添加一个空行后，用 `difflib.unified_diff` 生成
  `old_text → new_text` 的 unified diff，**不带行号**、以 `diff` 语法展示，
  并去掉开头的 `---`/`+++` 文件头两行（实时与重放一致）；当
  `old_text` 与 `new_text` 完全相同时 diff 为空，不追加代码块；
  - **优先整文件级 diff**：工具调用渲染时文件尚未被修改，读取 `file_path`
    当前内容（与应用逻辑一致：首个匹配或 `replace_all` 全部匹配）后，对
    「替换前 vs 替换后」的完整文件做 diff —— 行号即**原始文件真实行号**；
  - **回退片段级 diff**：路径越界 / 文件不存在或不可读 / `old_text` 为空 /
    未在文件中出现（可能已应用） / 非 `replace_all` 但匹配多处（工具本会
    拒绝执行）时，无法做整文件替换，回退到对两个片段直接 `unified_diff`
    （此时行号为片段行号、上下文仅片段自身）；
  - **历史重放（replay）不读文件**：`render_replay` 对 `ToolCallEvent`
    单独分支（`render_tool_call(tool_call, replay=True)`），replay 时文件
    可能已不存在或已改变，edit 一律不读实际文件、改用片段级 diff，并把
    无真实语义的 `@@` 行号替换为 `@@ ... @@`（`...` 表示行号不可知/
    省略）；其余工具类型照常委托 `_render_common`；
- 四个工具的 YAML 块均保留除大字段外的其余参数（bash 无多余字段时 YAML 块
  整体省略），当参数**无法解析成 dict**（非法 JSON / 顶层非对象）时无法
  特化，回退旧版行为——原样展示原始参数字符串（不再输出空 YAML 块）；
- 非特化工具（`read` / `ls` / `glob` / `grep` / `todo_write` 等）保持完整 YAML
  参数块展示，行为不变。

### 3.6 read 工具结果（带行号语法高亮）

default 风格下 `read` 工具返回（`tool_name == "read"` 且内容含行号）走
`render_read_output`，按以下规则渲染为带行号的语法高亮：

1. 若最后一行以 `...` 开头（截断 / 剩余提示），单独拿出来；
2. 其他带行号的行去掉行号（`^\s*\d+\t` 前缀），交给 rich 重新编号（行号连续、
   从真实初始行号 `_first_read_lineno` 开始）；
3. 若有第 1 步拿出的行，再补上输出（蓝灰 `\x1B[38;5;110m`，与深灰背景区分、柔和不刺眼）。

语言识别（默认风格）：优先按调用时登记的 `file_path` 用 Pygments
`guess_lexer_for_filename` 推断（扩展名如 `.py`/`.sh`/`.yaml`，或已知文件名如
`.bashrc` 直接命中）；文件名推断不到再按内容 `guess_lexer`；纯文本 / 猜不到
由 `_syntax_plain` 统一回退到 `markdown` 词法分析（保留行号，markdown 语法
如标题/代码块仍会着色）。

仅当「`tool_name == "read"` 且内容含行号」时走此路径；错误 / 越界等文本照常走
普通代码块渲染。

### 3.7 TODO 渲染（format_todos）

模板共用（`_TODO_FORMATS`），符号由子类提供：

| 状态 | default 符号 | classic 符号 | 标题样式 |
|------|--------------|--------------|----------|
| completed | `✅️` | `- [{x}]:`（x 绿） | 灰 + 删除线 |
| in_progress | `🟧` | `- [{>}]:`（> 橙） | 粗 + 白 |
| pending | `🔳` | `- [ ]:` | 普通 |

### 3.8 default 用户消息（灰色背景块）

`_DefaultRenderer.render_user_message` 是 default 风格最复杂的部分：

- 首行带提示符（`│` / `│?` / `│!`，用当前模式色），续行顶格无前缀（与输入区
  `prompt_continuation=''` 一致）；
- 按显示宽度自行分行（`_wrap_by_display_width`），每段**填充到终端宽度**，
  保证背景从消息第一行到最后一行全量覆盖——不依赖终端自动换行（自动换行后
  短段没有背景色，且宽字符在行尾的截断行为各终端不一致）；
- 宽字符（中文占 2 列）在行尾放不下时当前行补空格、字符转下行；
- 零宽字符（emoji 修饰符）跟随前一字符，不计宽度；
- 提示符竖线用模式色渲染，空格 + 正文恢复默认前景（`_FG_DEFAULT`），保留背景色。

### 3.9 classic 用户消息

单行：`{模式色}{myc[模式] >} \x1B[0m{text}`，末尾空行。

### 3.10 NoticeEvent 文案结构

`NoticeEvent` 承载系统级提醒（陈旧待办、命令修改等），文案分三部分
（`session.py`）：

| 字段 | 说明 |
|------|------|
| `content` | 发给 LLM 的提醒，`to_user_msg()` 用 `<tag_name>` 标签整体包裹后喂给模型 |
| `display_content` | 展示渲染用的提醒，非空时优先于 `content` |
| `additional_content` | 附加内容，如代码块，渲染与 LLM 均照常输出 |
| `tag_name` | 喂给模型时包裹 `content` 的标签名，需显式传入（陈旧提醒用 `reminder`，命令修改用 `notice`） |

`to_user_msg()` 把 `content` 整体包进 `<tag_name>` 标签，附加内容代码块
按原样跟在标签之后，避免整块被标签包裹。渲染时 `render_notice` 把提醒
文本整体用黄色高亮渲染，附加内容经 `render_notice_additional` 处理：
default 解析围栏语言后做语法高亮（围栏不保留），classic 原样输出。
仅当附加内容「整体恰好是一个完整代码块」时高亮：收尾围栏长度须 >= 开头围栏
长度（markdown 语义，避免正文短反引号行抢先闭合），且须为最后一行（围栏后
无其他内容）；正文中间的独立反引号行不会提前闭合。

典型场景「编辑 bash 命令」（`tag_name="notice"`）：
- 渲染标题：`命令修改为：`（`display_content`）
- LLM 标题：`用户将命令修改为：`（`content`）
- 附加内容（`additional_content`）：`` ```bash\n{新命令}\n``` ``
- 触发逻辑：编辑后命令与原文不一致才派发事件并注入模型；一致则直接执行。

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

确认界面（`mycode.confirm` 的 `Application`）同样设 `erase_when_done=True`：
确认 / 编辑界面退出时立即擦除自己渲染的画面，编辑完成的命令不会残留，
随后由 `NoticeEvent` 提醒统一展示。

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

1. `bus.dispatch(NoticeEvent)`（陈旧待办提醒 / 编辑命令已更新提醒，如有）
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
