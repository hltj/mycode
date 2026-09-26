# form_ui 通用表单界面设计

`mycode.form_ui` 是与具体业务解耦的通用**多字段表单输入**界面，用于供应商变量
配置（`/provider` 的 env 填写、自定义供应商表单）等场景复用。实现方式与
`ask_ui` 同类：基于 prompt_toolkit 独立 `Application`，不依赖渲染风格之外
的任何业务模块。

## 动机

「添加自定义供应商」这类表单几乎全是输入框（显示名 / base_url / api_key /
模型列表），`ask_ui` 的选项模型（选项 + 末尾自定义输入）并不适合逐字段
输入；因此提供专用的表单界面。

## 字段模型

```python
class FormField:
    name: str                 # 必填，字段标识（返回值 dict 的键）
    label: str                # 必填，展示标签
    initial: str = ""         # 初始值（回填现值用）
    placeholder: str = ""     # 占位文本（空值时灰显）
    password: bool = False    # True 时密码式掩码（显示为 * ）
    required: bool = False    # True 时空值不允许提交
    hint: str = ""            # 补充说明（随 label 展示，暗灰）
    validator: Callable[[str], str | None] | None = None
                              # 校验函数：返回 None 通过；
                              # 返回字符串为错误提示（阻止提交）
```

## 界面布局

```
表单标题（可选，ask-title 样式）
表单描述（可选，markdown 渲染，复用 ask_ui 的描述逻辑）

❯ 标签A: 已输入文本           ← 当前行（❯ + ask-active）

  标签B: <占位文本>           ← 其余行（两空格前缀）

  标签C（必填）: ****

↑↓ 切换字段 · ↵ 下一字段/提交 · Alt-↵（ESC ↵）提交 · Ctrl-C 取消   （暗灰，可选）
```

- 每行 = 前缀（2 列：`❯ ` 当前行 / `  ` 其余）+ label + `: ` + 输入框。
- **相邻字段行之间空一行**：输入区上下留出间距，不连成一块。
- 描述（`description` 入参）：标题与描述都存在时二者之间空一行（与
  ask_ui 的 header 一致）；描述也可单独存在（无标题时直接展示），
  支持多行；default 风格下 markdown 渲染（复用 `ask_ui` 的
  `_description_fragments`，含 rich soft_wrap + prompt_toolkit 折行策略）。
- 输入框即 `BufferControl`，仅当前行可聚焦；密码字段追加
  `PasswordProcessor` 掩码显示。
- label 行高固定 1；label 自身不做换行处理（调用方保证简短）。
- `hint` 非空时追加在 label 之后、冒号之前，用 `ask-description` 暗灰样式，
  形如 `标签C（必填）`。
- 字段区与底部之间留两行：一个空行 + 错误行（错误行空时以空行占位，
  布局稳定不跳动；有错误时以 `ask-unanswered` 亮黄显示），随后是提示行。
- 提交校验失败时在错误行显示首个错误，光标自动跳到出错字段。

## 交互

- `↑` / `↓` / `C-p` / `C-n`：切换字段（环形）。
- `Enter`：非末字段 → 跳到下一字段；末字段 → 校验全部字段，
  全部通过则提交退出；任一校验失败则显示错误并聚焦到第一个出错字段。
- `Alt-Enter`（实际为 `ESC` `Enter` 序列，即 `A-enter` / `escape enter`）：
  任意位置直接尝试提交（与末字段 Enter 相同的校验流程）。
- `Ctrl-C`：取消退出（`aborted=True`）。
- 输入字符只在当前字段的 Buffer 中生效（prompt_toolkit 天然行为）。

## 返回值

```python
class FormResult:
    values: dict[str, str]    # {字段 name: 输入值}；aborted 时为空 dict
    aborted: bool             # True 表示 Ctrl-C 取消
    focus_index: int          # 提交/取消时的焦点字段索引（回传恢复用）
```

## 状态持久化

`form_ui(fields, focus_index=...)` 支持注入初始焦点索引；返回的
`focus_index` 可在下次调用回传，维持「多次调用间焦点位置不变」。

## 样式

经 `Application.style` 透传，复用 renderer 已登记的样式类：

- `ask-title`：标题。
- `ask-active`：当前字段行（含前缀 `❯`）。
- `ask-description`：hint 与提示行。
- `placeholder`：占位文本（与 cli 输入框共用）。
- `mycode-input`：输入框背景（default 风格灰底）。

不新增样式类，两风格无需额外登记。

## 开发规范

- 测试以 TDD 方式编写（见 `tests/test_form_ui.py`）：用
  `create_pipe_input` 注入按键序列驱动，`DummyOutput` 输出。
