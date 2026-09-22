# ask_ui 通用询问界面设计

`mycode.ask_ui` 是与具体工具解耦的通用问答界面，供确认、后续 ask 工具等复用。

## 选项模型

```python
class AskOption:
    label: str                 # 必填，选项展示标题
    value: Optional[str]       # 取值；无 value 回退到 label
    description: Optional[str] # 描述（自定义选项作占位文本）
    is_custom: bool            # 是否为「自定义输入」选项
```

- `AskOption(label, value?, description?, is_custom?)`；`value` 缺失时回退到 `label`。
- 末尾选项建议设 `is_custom=True`，标签后追加输入框，占位文本取该选项的 `description`。

## 多问题模式

`ask_ui` 只接受 `questions: list[AskQuestion]` 一个入参（单/多问题统一用数组）；每个问题的数据在 `AskQuestion` 中：

```python
class AskQuestion:
    title: str = ""                  # 问题短标题（标题行横向展示；仅单问题可空串）
    description: str = ""            # 完整问题描述（当前问题正下方）
    options: list[AskOption] | None  # 选项列表（规则同 ask_ui options）
    multi: bool = False              # 该问题是否多选
    custom_buffer: Buffer | None     # 复用该问题自定义输入框 Buffer
    cursor_index: int = 0            # 初始焦点选项索引
    checked: set[int] | None         # 多选初始勾选集合
```
- 多问题（`len(questions) > 1`）触发多问题 UI；单问题即长度 1 的数组，与多问题数据结构一致，仅 UI 和交互有差异。

### 多问题交互

- 顶部标题行横向排列所有问题**短标题**，末尾追加「提交」。
- 每个标题前有复选框，风格与多选选项的复选框一致：default 为 `✅`/`🔳`，classic 为 `[x]`/`[ ]`，默认未勾选，问题选定答案后置勾选。
- 当前标题用 `ask-title`（标题色），其余为普通文本颜色；末尾「提交」在导航到预览页时也用标题色。
- 标题行可视为 `[Q1] [Q2] ... [Qn] [提交]` 的**闭环**：左右键（`Left`/`Right`，及 `C-b`/`C-f`）、`tab` 在问题与提交预览页间切换，最后一个问题向右 / 第一个问题向左均切到提交预览页，预览页向右回到第一个问题、向左回到最后一个问题，与提交页的左右键衔接；**自定义输入框激活且焦点在其中时左右键留给输入框移动光标**（`tab` 不受此限制，始终像向右一样推进）。
- 每个问题内 `Up`/`Down` 移动选项，`Enter` 选定当前焦点项（单选）或全部勾选项（多选）并切到下一问题；最后一个问题 `Enter` 后进入提交预览页。
- 预览页列出每个问题短标题及所选答案；未作答（未按 `Enter` 选定）的以亮黄（`ask-unanswered`）显示「未回答」。
- 预览页下方是「确认 / 取消」单选（无标题与描述，仅两个选项）：确认返回全部答案；取消即整体取消回答（`aborted=True`）。

## 交互方式

- 单选 / 多选由 `multi` 参数切换（多选用 `Space` 切换勾选、`Enter` 提交全部）。自定义输入框激活时 `Space` 始终作为普通空格输入（单选下焦点到自定义行即激活）；多选下的自定义输入框需先用 `Space` 选中激活，光标移到输入框最左按 `Backspace` 可失活并取消选中（`Space` 恢复为切换勾选）。单选下普通选项的 `Space` 无操作。
- 标题与描述都可选：为空时跳过对应行（confirm 不传二者）。
- 描述（问题描述）支持**多行**：真实换行按行展开，行高随内容自动决定；行数不影响焦点行索引（描述在根布局中始终只占一个 child 槽位）。default 风格下描述还支持 **markdown 渲染**（加粗 / 内联代码 / 列表 / 代码块等，复用 `renderer._markdown_ansi` 的 rich 渲染）；classic 保留纯文本（仍支持多行）。
- **描述换行策略（default）**：宽度断行不依赖 rich 的词级折行（rich 按空白分词，连续中文会整体划为一块，当超出行宽时整体移到下一行），而是：
  1. `renderer._markdown_ansi(..., soft_wrap=True)` 让 rich **不折行**（超宽行保持单行，同时保留段落/代码块结构，换行交给下游）；
  2. 描述 Window 设 `wrap_lines=True`，由 prompt_toolkit 逐字符按显示宽度折行——中文可**任意汉字换行**；
  3. `_strip_trailing_pad` 剥离 rich 块级元素行尾补满的背景 pad 空格，避免短列表项被误判为整宽而错误折行。
- **描述换行语义**：ask_ui 不做任何文本改写，换行完全遵循标准 markdown 语义——相邻行（无空行分隔）会被 rich 折叠进同一段落（换行变空格）；需要显式换行时，入参应使用 hard break（行尾两个空格，`line1  \nline2`），与 `PromptSession` / 普通 markdown 渲染一致。
- 普通选项展示 `label`；`description` 非空时同行展示。
- 自定义选项：`label` 后追加输入框，输入框占位文本为 `description`。

## 状态持久化

支持多次调用间维持用户状态，按保存的状态恢复 UI：

- `AskQuestion.custom_buffer` 可注入已存在的 `Buffer` 实例；多次调用复用同一 buffer 时，文本与光标位置在调用间保留。
- `AskQuestion.cursor_index` / `checked` 入参可注入初始焦点位置 / 勾选集合；对应答案的 `cursor_index` / `checked` 反映提交时的状态，可回传以恢复。

## 返回值

`ask_ui` 返回整体结果 ``AskResult``，纯数组结构（无任何特化字段）：

```python
class AskResult:
    answers: list[AskAnswer]  # 每道问题的答案，顺序与问题数组一致
    aborted: bool             # True 表示整体中止（Ctrl-C 或预览页「取消」）

class AskAnswer:              # 单道问题的答案
    selected: list[str]       # 选中项 value 列表（无 value 回退 label）
    input: Optional[str]      # 选中自定义选项时为输入框文本（可为空串），否则 None
    cursor_index: int         # 提交时焦点所在选项索引
    checked: set[int]         # 多选勾选集合
    skipped: bool             # True 表示该问题未作答（未按 Enter 选定）
```

- ``answers``：长度与问题数一致的答案数组；单问题即长度 1 的数组。
  **未作答**的问题（用户未按 `Enter` 选定就在提交预览页确认提交）
  对应答案 ``skipped=True``、``selected`` 为空列表、``input`` 为
  ``None``；多选下主动勾选 0 项后提交的，``skipped=False`` 且
  ``selected`` 也为空列表。
- ``aborted``：``True`` 表示用户以 Ctrl-C 终止交互，或多问题预览页选了
  「取消」；此时 ``answers`` 为空列表。调用方应**优先以 ``aborted``** 判断
  是否取消（confirm 以此判定）。
- 每题答案的 ``cursor_index`` / ``checked`` 可在下次调用时回传给对应的
  ``AskQuestion`` 维持状态。

## 焦点控制

- 自定义输入框的焦点由"激活态"决定：单选下跟随 `sel`（焦点在自定义行即激活）；多选下需被 `Space` 显式选中才激活（`custom_idx in checked`），未选中时输入框不可聚焦，避免 prompt_toolkit 默认聚焦第一个 focusable 控件。
- 普通选项行（当前选中项）：`FormattedTextControl` 设为 `focusable=True` 且 `show_cursor=False`——焦点落在当前选中行而不是标题等首行，但因不是输入控件不闪现光标。
- 其他选项上输入字符被丢弃，避免焦点残留污染。
- **描述多行不影响焦点行计算**：焦点行用的是「根 HSplit 的 child 下标」而非屏幕行号；描述无论多少行都在根布局里占一个 child 槽位（见 `_option_row_offset`），`_focused_window` 仍按 `_option_row_offset + sel` 准确取到当前选中行。

## 样式

样式经 `Application.style` 透传，需由调用方传入。`ask-title` / `ask-question` / `ask-description` / `ask-active` / `ask-unanswered` 在 renderer 两风格样式表中登记：

- `ask-title`（标题）：`bold #00c099`。
- `ask-question`（问题描述）：粗体（`bold`）。
- `ask-description`（选项描述）：`fg:#6F6F6F`（暗灰）。
- `ask-active`（当前选中行）：`bold #009fff`。
- `ask-unanswered`（预览未回答）：`bold #ffff00`（亮黄，同默认渲染的提醒色）。

问题描述窗口（default markdown / classic 纯文本）整体挂 `ask-question`：未着色的纯文本部分继承粗体，markdown 富文本样式（加粗 / 内联代码 / 代码块等）叠加生效。选项描述（普通选项行尾、自定义选项占位）沿用 `ask-description` 暗灰，与问题描述区分。

- **占位文本**：用 `AfterInput + ConditionalProcessor`（与 `PromptSession.placeholder` 同款机制）渲染 `opt.description`（前导一个空格给光标留可视距离）；样式类 `class:placeholder` 与 cli 输入框共用，`italic fg:#666666` 暗灰斜体。
- **自定义输入框背景**：挂 `class:mycode-input`，default 风格下 `bg:#333333`（与 cli 提示词输入区一致），classic 风格为空（保持原风格）。

### 选项前缀

选项行前缀按渲染风格区分（`_mark_str` 读取 `renderer.RENDER_STYLE`），统一 5 列宽以对齐标签：

- **默认风格**：单选当前行 `❯ 🟢 `、其余 `  ⚪ `；多选左侧指示当前行（`❯ `/`  `），勾选态用 `✅ `/`🔳 `（如 `❯ ✅ `、`  🔳 `）。
- **传统风格（classic）**：单选当前行 `> `、其余 `  `；多选左侧 `> `/`  ` + 勾选态 `[x] `/`[ ] `（如 `> [ ] `、`  [x] `）。

## 开发规范

- 新增渲染样式：`ask_ui` 相关样式（`ask-title` / `ask-question` / `ask-description` / `ask-active` / `ask-unanswered`）在 renderer 两风格样式表登记；占位文本复用 `PromptSession.placeholder` 已有的 `class:placeholder`，`mycode-input` 也复用 cli 输入区已有样式类（两风格均登记，default 加灰色背景，classic 留空保持原风格）。
- 测试：优先以 TDD 方式为交互 / 布局 / 状态持久化写测试（见 `tests/test_ask_ui.py`）。