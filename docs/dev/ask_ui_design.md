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

- `custom_buffer` 参数可注入已存在的 `Buffer` 实例；多次调用复用同一 buffer 时，文本与光标位置在调用间保留。
- `cursor_index` / `checked` 入参可注入初始焦点位置 / 勾选集合；返回值中对应字段反映提交时的状态，可回传以恢复。

## 返回值

调用方拿到 ``AskResult`` 数据类（``selected`` / ``input`` / ``cursor_index`` /
``checked`` / ``aborted`` 五个字段）：

- ``selected``：选中项 `value` 列表（无 `value` 回退 `label`）；单选长度 1，多选按 options 顺序列出勾选项。
- ``input``：仅当选中自定义选项时为输入框当前文本（可能为空串），未选则为 `None`。
- ``cursor_index``：提交时焦点所在选项索引（供下次调用维持焦点位置）。
- ``checked``：多选模式下提交时的勾选集合（供下次调用维持勾选）。
- ``aborted``：`True` 表示用户以 Ctrl-C 终止交互；此时 `selected` 为空列表、`input` 为 `None`。调用方应**优先以 `aborted` 判断是否取消**，而不是依据 `selected` 是否为空（confirm 兼容两种方式）。

## 焦点控制

- 自定义输入框的焦点由"激活态"决定：单选下跟随 `sel`（焦点在自定义行即激活）；多选下需被 `Space` 显式选中才激活（`custom_idx in checked`），未选中时输入框不可聚焦，避免 prompt_toolkit 默认聚焦第一个 focusable 控件。
- 普通选项行（当前选中项）：`FormattedTextControl` 设为 `focusable=True` 且 `show_cursor=False`——焦点落在当前选中行而不是标题等首行，但因不是输入控件不闪现光标。
- 其他选项上输入字符被丢弃，避免焦点残留污染。
- **描述多行不影响焦点行计算**：焦点行用的是「根 HSplit 的 child 下标」而非屏幕行号；描述无论多少行都在根布局里占一个 child 槽位（见 `_option_row_offset`），`_focused_window` 仍按 `_option_row_offset + sel` 准确取到当前选中行。

## 样式

样式经 `Application.style` 透传，需由调用方传入。`ask-title` / `ask-description` / `ask-active` 在 renderer 两风格样式表中登记：

- `ask-title`（标题）：`bold #00c099`。
- `ask-description`（问题描述 / 选项描述）：`fg:#6F6F6F`。
- `ask-active`（当前选中行）：`bold #009fff`。

描述窗口（default markdown / classic 纯文本）整体挂 `ask-description`：未着色的纯文本部分继承暗灰描述色，markdown 富文本样式（加粗 / 内联代码 / 代码块等）叠加生效。

- **占位文本**：用 `AfterInput + ConditionalProcessor`（与 `PromptSession.placeholder` 同款机制）渲染 `opt.description`（前导一个空格给光标留可视距离）；样式类 `class:placeholder` 与 cli 输入框共用，`italic fg:#666666` 暗灰斜体。
- **自定义输入框背景**：挂 `class:mycode-input`，default 风格下 `bg:#333333`（与 cli 提示词输入区一致），classic 风格为空（保持原风格）。

### 选项前缀

选项行前缀按渲染风格区分（`_mark_str` 读取 `renderer.RENDER_STYLE`），统一 5 列宽以对齐标签：

- **默认风格**：单选当前行 `❯ 🟢 `、其余 `  ⚪ `；多选左侧指示当前行（`❯ `/`  `），勾选态用 `✅ `/`🔳 `（如 `❯ ✅ `、`  🔳 `）。
- **传统风格（classic）**：单选当前行 `> `、其余 `  `；多选左侧 `> `/`  ` + 勾选态 `[x] `/`[ ] `（如 `> [ ] `、`  [x] `）。

## 开发规范

- 新增渲染样式：`ask_ui` 相关样式（`ask-title` / `ask-description` / `ask-active`）在 renderer 两风格样式表登记；占位文本复用 `PromptSession.placeholder` 已有的 `class:placeholder`，`mycode-input` 也复用 cli 输入区已有样式类（两风格均登记，default 加灰色背景，classic 留空保持原风格）。
- 测试：优先以 TDD 方式为交互 / 布局 / 状态持久化写测试（见 `tests/test_ask_ui.py`）。