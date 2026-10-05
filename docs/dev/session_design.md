# mycode 会话管理设计

## 概述

mycode 的会话系统负责**事件持久化 + 内存存储 + 启动安全校验**。

核心文件：
- `session.py` — SessionHistory（持久化）、事件 ADT 类型、信任检查函数
- `cli.py` — 目录信任确认入口 `_check_dir_trust()`

---

## 一、SessionHistory

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
- `get_messages()` — 过滤出可发的消息（含 ToolResultEvent.to_tool_msg / NoticeEvent.to_user_msg，排除 session/interrupt/tool_call/exception；assistant 消息统一剥离 `reasoning_content`）

### 思考内容 reasoning_content

思考类模型会在 assistant 消息里返回非标准字段 `reasoning_content`
（真实响应中落在 pydantic 的 `model_extra` 里）。该字段始终**持久化并
渲染**，并随 `messages` / `get_messages()` 原样保留。是否回传由**当前
模型**的回传开关决定（`/provider` 的「模型配置」维护，未配置时按
models.dev 的 `interleaved` 推导）：开关开启则原样回传；关闭则在**请求发出
前**由 `strip_reasoning_all()` 统一剥离——该字段不是 openai 标准字段，
部分提供商收到会直接报错。剥离只发生在 `cli.agent_loop` 调用
`chat.completions.create` 的传参处（请求离开进程的唯一出口），会话与内存
里的思考内容不受影响，之后切回支持回传的模型仍能带上。

```python
def extract_reasoning(message: Any) -> str: ...   # 从模型响应对象取思考内容（缺失/非字符串→""）
def attach_reasoning(message, reasoning) -> ...  # 给消息附加 reasoning_content（空值原样返回，不修改入参）
def get_reasoning(message) -> str               # 从已构造的消息 dict 取思考内容（渲染用）
def _strip_reasoning(message) -> ...            # 发回模型前移除该字段
```

- `agent_loop` 从响应取出思考内容后：`messages` 与派发的 `AssistantMessage`
  （持久化 + 渲染）都**带上**该字段，不在此处按开关剥离；
- 回传开关在 `create` 传参处统一生效：开启时传原列表，关闭时传
  `strip_reasoning_all(messages)`（新列表，不改内存里的消息对象），
  历史与本轮消息一并按**当前模型**判断；
- 渲染规则见 [CLI 渲染设计](./cli_render_design.md)。

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
{"time":"...","type":"message","id":"...","parent_id":"...","model":"gpt-4o","message":{"role":"assistant","content":"done","reasoning_content":"思考内容（可选，仅持久化，不发回模型）"}}
{"time":"...","type":"interrupt","id":"...","parent_id":"...","model":"gpt-4o","provider":"openai","interrupt":{"abort":true}}
{"time":"...","type":"notice","id":"...","parent_id":"...","model":"gpt-4o","provider":"openai","notice":{"content":"...","tag_name":"notice","display_content":"...","additional_content":"..."}}
{"time":"...","type":"tool_result","id":"...","parent_id":"...","model":"gpt-4o","provider":"openai","tool_result":{"tool_call_id":"...","content":"...","tool_name":"bash"}}
```

**序列化规范**：

1. 只有 `MessageProtocol` 定义的公共字段（`time` / `type` / `id` /
   `parent_id` / `model` / `provider` / `mode`）平铺在 JSON 顶层；
2. 每个事件**自己的扩展字段**聚合在 JSON 中 `type` 值对应的 key 下
   （`session` / `message` / `tool_call` / `interrupt` / `exception` /
   `notice` / `tool_result` / `model_change`），即扩展字段的 key 名与
   `type` 值一致；
3. `parent_id` 链构成完整的消息树。

### ID 生成策略

1. 生成完整 UUID
2. 取前 8 位作为短 ID
3. 与内存中所有已有 id 比对，不冲突则用短 ID，冲突则用完整 36 位 UUID
4. 比对范围仅为**内存 entries**，不读文件（load 时 entries 已完整恢复）

---

## 二、目录信任确认

mycode 启动时通过 `_check_dir_trust()`（定义在 `cli.py`）检查当前目录是否已被信任。

### 信任检查流程

1. `main()` 设置渲染风格后调用 `_check_dir_trust()`
2. 调用 `is_dir_trusted(cwd)` 检查是否已信任
3. 已信任则直接返回，未信任则通过 `ask_ui()` 弹出确认界面
4. 用户选择"信任"时调用 `trust_dir(cwd)` 记录，选择"不信任"或 Ctrl-C 时 `sys.exit(1)`

### 信任状态存储

- `session.py` 提供三个函数：
  - `_get_dirs_file(dir_path)` — 获取 `.dirs` 文件路径（`SESSIONS_DIR / sanitized_cwd / ".dirs"`）
  - `is_dir_trusted(dir_path)` — 检查 `.dirs` 中是否已包含该目录的绝对路径
  - `trust_dir(dir_path)` — 将目录绝对路径追加到 `.dirs`（幂等，重复调用不写入多次）

---

## 三、开发规范

1. **SessionHistory 使用**：通过 AgentEventBus dispatch 注入元数据后自动 append；直接调用 append（如测试场景）必须先手动调用 `inject_meta()`
2. **entries 完整性**：内存 entries 包含 SessionRecord 在内的所有事件类型；`get_messages()` 负责过滤出可发给 LLM 的消息，排除 session/interrupt/tool_call/exception
3. **JSONL 序列化**：公共字段平铺在 JSON 顶层，扩展字段聚合在 `type` 值对应的 key 下（key 名与 type 值一致）
4. **ID 生成**：仅比对内存 entries，不读文件；短 ID 冲突时降级为完整 36 位 UUID
5. **信任状态**：`is_dir_trusted()` / `trust_dir()` 是幂等的，重复调用不会产生副作用；信任状态存储在 `.dirs` 文件，每行一个目录绝对路径
6. **reasoning_content**：始终持久化 + 渲染；`messages` / `get_messages()` 原样保留该字段，剥离只发生在 `cli.agent_loop` 的 `create` 传参处，按**当前模型**的回传开关决定
