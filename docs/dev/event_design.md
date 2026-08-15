# mycode 事件架构设计

## 概述

mycode 采用 **事件驱动 + 观察者模式** 的架构：所有交互动作（用户输入、AI 回复、工具调用、中断、异常等）统一建模为 `AgentMessage` 类型，通过 `AgentEventBus` 分发，由注册的处理器（Handler）分别处理渲染与持久化。

核心文件：
- `session.py` — ADT 类型定义、MessageProtocol、SessionHistory（持久化）
- `myc.py` — AgentEventBus、渲染处理器、主循环

---

## 一、ADT：AgentMessage 联合类型

所有交互事件统一为 `AgentMessage` 联合类型，定义在 `session.py`：

```python
AgentMessage = SessionRecord | UserMessage | AssistantMessage | ToolCallEvent | ToolResultEvent | InterruptEvent | ExceptionEvent | ModeChangeEvent | NoticeEvent
```

### MessageProtocol

所有子类型显式继承 `MessageProtocol`，共享元数据字段：

```python
class MessageProtocol(Protocol):
    id: str                  # 记录 ID（短 ID，通常为 UUID 前 8 位）
    parent_id: Optional[str] # 前一条记录的 id，构成链表
    model: str               # 使用的模型名称
    entry_type: str          # session / message / tool_call / tool_result / interrupt / exception / mode_change / notice
    time: str                # ISO8601 时间戳
```

各字段的**默认值均为空**（`id=""`, `parent_id=None`, `time=""`），在 dispatch 时由 AgentEventBus 统一注入。

### 子类型一览

| 类型 | 业务字段 | entry_type | 说明 |
|------|---------|------------|------|
| `SessionRecord` | `session: SessionData` | `"session"` | 会话初始化记录 |
| `UserMessage` | `message: ChatCompletionUserMessageParam` | `"message"` | 用户输入 |
| `AssistantMessage` | `message: ChatCompletionAssistantMessageParam` | `"message"` | AI 回复 |
| `ToolCallEvent` | `tool_call: ChatCompletionMessageFunctionToolCallParam` | `"tool_call"` | 工具调用发起 |
| `ToolResultEvent` | `tool_result: ToolResultData`（tool_call_id / content / tool_name） | `"tool_result"` | 工具执行结果 |
| `InterruptEvent` | `abort: bool` | `"interrupt"` | Ctrl-C 中断（abort 标记取消/无理由拒绝） |
| `ExceptionEvent` | `exception: ExceptionData` | `"exception"` | 异常 |
| `ModeChangeEvent` | （无额外字段，模式见公共 `mode`） | `"mode_change"` | 模式切换 |
| `NoticeEvent` | `content` / `display_content` / `additional_content` / `tag_name` | `"notice"` | 系统级提醒注入 |

> **注意**：`entry_type` 在 dataclass 中通过默认值硬编码，不可修改。联合类型顺序固定为 SessionRecord → UserMessage → ... → InterruptEvent → ExceptionEvent，所有 match-case 必须按此顺序处理并在末尾添加 `case _ as unreachable: assert_never(unreachable)` 确保 exhaustiveness。

---

## 二、AgentEventBus

事件总线负责**元数据注入 + 分发**。

```python
class AgentEventBus:
    def __init__(self, session_hist: SessionHistory | None = None) -> None:
        self._handlers: list[Handler] = []
        self._session_hist = session_hist

    def dispatch(self, msg: AgentMessage) -> None:
        if self._session_hist is not None:
            self._session_hist.inject_meta(msg)  # 注入 id/parent_id/time
        for handler in self._handlers:
            handler(msg)  # 处理器按注册顺序依次收到消息
```

### 元数据注入流程

1. **构造消息时只传业务字段**：`UserMessage(model=model, message=user_msg)`
2. **dispatch 时自动注入**：bus 调用 `session_hist.inject_meta()` 设置 `id`、`parent_id`、`time`
3. `inject_meta()` 内部逻辑：
   - `msg.id = self._next_id()` — 生成不冲突的短 ID（8 位 UUID，冲突时用完整 36 位）
   - `msg.parent_id = self.entries[-1].id` — 指向内存中上一条记录
   - `msg.time = get_iso_timestamp()` — 当前时间

### 两种使用场景

#### 实时交互（CLI）

```python
bus = AgentEventBus(session_hist=session_hist)
bus.register(make_persist_handler(session_hist))
bus.register(render_terminal)
# dispatch → inject_meta → 持久化处理器 → 渲染处理器
```

#### 历史重放（Replay）

```python
bus_replay = AgentEventBus()  # 不传 session_hist，跳过 inject_meta
bus_replay.register(render_replay)
for entry in session_hist.entries:
    bus_replay.dispatch(entry)
```

重放时 entries 中的消息已有完整的 id/parent_id/time，无需再次注入。

---

## 三、SessionHistory

会话历史记录管理器，负责**文件持久化 + 内存存储**。

### 核心接口

```python
class SessionHistory:
    def inject_meta(self, msg: AgentMessage) -> None
    def append(self, msg: AgentMessage) -> None
    def get_messages(self) -> List[ChatCompletionMessageParam]
```

- `inject_meta()` — 注入元数据（id/parent_id/time）
- `append()` — 写入文件并追加到内存 entries（**调用前必须已注入元数据**）
- `get_messages()` — 过滤出可发的消息（含 ToolResultEvent.to_tool_msg / NoticeEvent.to_user_msg，排除 session/interrupt/tool_call/exception）

### 内存结构

`entries: List[AgentMessage]` 存储所有记录，**包含 SessionRecord**。

```
entries[0] = SessionRecord     # 会话初始化
entries[1] = UserMessage       # 用户输入
entries[2] = AssistantMessage  # AI 回复
entries[3] = ToolCallEvent     # 工具调用
entries[4] = ToolResultEvent   # 工具结果
...
```

### JSONL 文件格式

每行一条 JSON 记录，结构统一：

```json
{"time":"...","type":"session","id":"a1b2c3d4","parent_id":null,"model":"gpt-4o","session":{"id":"full-uuid...","cwd":"/path"}}
{"time":"...","type":"message","id":"e5f6g7h8","parent_id":"a1b2c3d4","model":"gpt-4o","message":{"role":"user","content":"hello"}}
{"time":"...","type":"interrupt","id":"...","parent_id":"...","model":"gpt-4o","interrupt":{"abort":true}}
{"time":"...","type":"notice","id":"...","parent_id":"...","model":"gpt-4o","notice":{"content":"...","tag_name":"notice","display_content":"...","additional_content":"..."}}
{"time":"...","type":"tool_result","id":"...","parent_id":"...","model":"gpt-4o","tool_result":{"tool_call_id":"...","content":"...","tool_name":"bash"}}
```

**序列化规范**：

1. 只有 `MessageProtocol` 定义的公共字段（`time` / `type` / `id` /
   `parent_id` / `model` / `mode`）平铺在 JSON 顶层；
2. 每个事件**自己的扩展字段**聚合在 JSON 中 `type` 值对应的 key 下
   （`session` / `message` / `tool_call` / `interrupt` / `exception` /
   `notice` / `tool_result`），即扩展字段的 key 名与 `type` 值一致；
3. `parent_id` 链构成完整的消息树。

### ID 生成策略

1. 生成完整 UUID
2. 取前 8 位作为短 ID
3. 与内存中所有已有 id 比对，不冲突则用短 ID，冲突则用完整 36 位 UUID
4. 比对范围仅为**内存 entries**，不读文件（load 时 entries 已完整恢复）

---

## 四、渲染处理器（渲染器架构）

渲染按渲染风格（`--style`，默认 `default`）拆分为不同的渲染器（处理器），
公共渲染流程在基类复用，风格差异由子类覆写。

### 渲染器基类 \_Renderer

```python
class _Renderer:
    # 公共流程（基类实现，复用给所有风格）
    def format_todos(self, state=None) -> str: ...
    def render_tool_call(self, tool_call) -> None: ...   # YAML 参数 + 代码围栏
    def render_tool_result(self, tool_result) -> None: ...
    def render_notice(self, notice) -> None: ...
    def render_exception(self, exc) -> None: ...         # traceback 围栏
    def render_interrupt(self) -> None: ...

    # 风格差异（子类覆写）
    def ai_title(self, model) -> str: ...
    def tool_call_title(self, func_name) -> str: ...
    def tool_result_title(self) -> str: ...
    def notice_text(self, content) -> str: ...
    def exception_title(self, exc_type, exc_message) -> str: ...
    def render_user_message(self, text) -> None: ...
    def prompt_fragments(self) -> list[tuple[str, str]]: ...
    def create_prompt_style(self) -> Style: ...
    def apply_input_style(self, session) -> None: ...
```

### 子类

- `_DefaultRenderer` — 默认风格：emoji 标题（AI `🤖 模型`、
  工具调用 `🔧 调用工具 - x`、工具输出 `📤 工具输出`、提醒 `💡`、异常 `❌`）、
  灰色输入区 + 首行竖线提示符。
- `_ClassicRenderer` — 经典风格：`AI【模型】`、复选框待办、`myc > ` 提示符。

### \_render_common（共享分发）

```python
def _render_common(msg: AgentMessage) -> None:
    renderer = _get_renderer()   # 按 RENDER_STYLE 惰性实例化并缓存
    match msg:
        case SessionRecord(): pass
        case UserMessage(message): renderer.render_user_message(content)
        case AssistantMessage(message, model): ...  # renderer.ai_title + 正文
        case ToolCallEvent(tool_call): renderer.render_tool_call(tool_call)
        case ToolResultEvent(tool_result): renderer.render_tool_result(tool_result)
        case InterruptEvent(): renderer.render_interrupt()
        case NoticeEvent(notice): renderer.render_notice(notice)
        case ExceptionEvent(exception): renderer.render_exception(exception)
        case _ as unreachable: assert_never(unreachable)
```

### render_terminal（实时交互）

委托给 `_render_common`，UserMessage 由 prompt_toolkit 显示故跳过。

### render_replay（历史重放）

```python
def render_replay(msg: AgentMessage) -> None:
    match msg:
        case InterruptEvent():
            print("^C")
            print()  # 模拟实时 Ctrl-C 的视觉表现
        case _:
            _render_common(msg)
```

> SessionRecord 由 `_render_common` 中的第一个 case 处理并跳过。

---

## 五、持久化处理器

```python
def make_persist_handler(session_hist: SessionHistory) -> Handler:
    def persist(msg: AgentMessage) -> None:
        session_hist.append(msg)
    return persist
```

极简实现——AgentEventBus dispatch 已注入元数据，处理器只需调用 append。

---

## 六、开发规范

1. **新增事件类型**：在 `session.py` 中添加新的 `@dataclass` 继承 `MessageProtocol`，放入 `AgentMessage` 联合类型中适当位置（SessionRecord 始终第一，ExceptionEvent 始终最后）
2. **match-case 顺序**：必须与联合类型顺序一致，末尾必须加 `case _ as unreachable: assert_never(unreachable)`
3. **构造消息**：只传业务字段 + model，id/parent_id/time 由 bus dispatch 自动注入
4. **直接调用 append**（如测试场景）：必须先手动调用 `inject_meta()`
5. **entries 包含 SessionRecord 等全部类型**：过滤消息时 `UserMessage` / `AssistantMessage` 直接取 `message`，`ToolResultEvent` / `NoticeEvent` 则分别经 `to_tool_msg()` / `to_user_msg()` 转成 OpenAI 消息（见 `get_messages`）
6. **render_replay 委托原则**：若某事件在 render_replay 中的行为与 `_render_common` 完全相同，则不在 render_replay 中单独处理，统一交由 `_render_common` 处理
