# filter_ui 通用筛选选择界面设计

`mycode.filter_ui` 是与具体业务解耦的通用**分页 + 关键词筛选**选择界面，
用于候选项较多的场景（如 models.dev 的 182 家供应商、单家数百个模型）。
实现方式与 `ask_ui` / `form_ui` 同类：基于 prompt_toolkit 独立
`Application`，不依赖业务模块。

## 动机

`ask_ui` 一次性渲染全部选项，适合少量选项；供应商选择面对 182 家候选、
模型选择可能面对数百项，需要关键词筛选（缩小列表）与分页（限制可视高度）。

## 选项模型

```python
class FilterOption:
    label: str                 # 必填，选项展示文本（筛选匹配对象）
    value: Optional[str]       # 取值；无 value 回退 label
    description: Optional[str] # 可选，展示在 label 之后（暗灰；不参与匹配）
    selected: bool = False     # 多选模式的初始勾选态
```

## 界面布局

```
标题（可选，ask-title 样式）

描述（可选，markdown 渲染，复用 ask_ui 的 _description_fragments）

筛选: 输入框                    ← 关键词输入（实时过滤，不区分大小写）

❯ ✅ 选项A  附加说明
  🔳 选项B  附加说明
  …（当前页，每页 page_size 行，默认 15）
第 1/3 页 · 共 42 项             ← 分页状态行（暗灰）

↑↓ 移动 · 空格 勾选/选择 · ←→ PgUp PgDn 翻页 · ↵ 确定 · Ctrl-C 取消   （暗灰）
```

- 标题与描述都存在时，二者之间空一行（与 ask_ui / form_ui 的 header 一致）。
- 筛选输入框始终可聚焦（`class:mycode-input` 背景），占位文本
  “输入关键词筛选”（`class:placeholder`）。
- 选项行前缀与 ask_ui 多选/单选风格一致：default 为 `❯ ✅ `/`❯ 🔳 `
  （多选勾选态）或 `❯ 🟢 `/`  ⚪ `（单选）；classic 为 `> [x] `/`> [ ] `
  或 `> `/`  `。
- 分页状态行显示 `第 x/y 页 · 共 n 项`（n 为过滤后数量；无匹配时
  `共 0 项`，选项区显示一行暗灰“无匹配项”）。

## 焦点模型

两个可聚焦区域——筛选输入框与当前选项行：

- 默认焦点在筛选框。
- 筛选框内 `↑`/`↓` 用于切换焦点：`↓` 进入选项列表（光标在当前页首项），
  `↑` 无操作；`←`/`→` 移动输入光标（编辑关键词），`pgup`/`pgdown` 翻页。
- 列表内 `↑`/`↓` 移动选项光标（只在当前页内环形：到底回到页首）；
  在第一个选项上按 `↑` 回到筛选框；`←`/`→` 翻页；`pgup`/`pgdown` 翻页。
- `tab` 在筛选框与列表间切换焦点。
- 焦点在列表时输入的字符被丢弃（关键词编辑只在筛选框内）。

## 过滤规则

- 关键词按“不区分大小写的子串匹配”作用于 `label`（不含 description）。
- 关键词变化时：页码重置为 1、光标重置到第一个选项（若焦点已在列表）。

## 交互

- `space`：多选模式切换当前项勾选；单选模式选定当前项（等价 Enter）。
- `enter`：筛选框内回车切到选项列表（过滤后列表为空则不切换）；
  列表内回车单选返回当前项、多选提交全部勾选项。
  过滤后列表为空时列表内 Enter 无操作（界面不退出）。
- `Ctrl-C`：取消返回（`aborted=True`）。

## 返回值

```python
class FilterResult:
    selected: list[str]        # 选中项 value 列表（无 value 回退 label）；
                               # 单选长度 0 或 1，多选按过滤后列表顺序
    aborted: bool              # True 表示 Ctrl-C 取消（selected 为空）
    keyword: str               # 提交/取消时的筛选关键词（回传恢复用）
    cursor_index: int          # 提交/取消时光标在“过滤后列表”中的索引
    page: int                  # 提交/取消时的页码（1-based）
    checked: set[int]          # 多选提交时的勾选集合（过滤后列表索引）
```

## 状态持久化

`filter_ui(options, keyword=..., cursor_index=..., keyword_buffer=...)`
支持回传恢复：`keyword` 恢复筛选词、`cursor_index` 恢复光标位置，
`keyword_buffer` 注入既有关键词输入框 `Buffer`（文本与光标位置跨调用
保留，与 ask_ui 的 `custom_buffer` 同款机制）；多选勾选态直接通过
`FilterOption.selected` 传入（多次调用间复用同一 options 列表即可）。

## 样式

复用 renderer 已登记样式类：`ask-title` / `ask-question`（描述）/
`ask-description`（选项 description、状态行、提示行）/ `ask-active`
（当前选项行）/ `placeholder` / `mycode-input`。不新增样式类。

## 开发规范

- 测试以 TDD 方式编写（见 `tests/test_filter_ui.py`）：`create_pipe_input`
  注入按键 + `DummyOutput`，覆盖过滤/分页/焦点切换/单多选/取消/布局。
