#!/usr/bin/env python3

import os
import sys
import json
import argparse
from typing import Any, Callable, cast

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 加载 .env 环境变量（须在导入本地模块前，本地模块顶部常量会读取环境变量，
# 例如 renderer 的语法高亮主题 MYCODE_SYNTAX_THEME）
# ---------------------------------------------------------------------------
load_dotenv()

from openai import OpenAI, RateLimitError
from openai.types.chat import (
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
    ChatCompletionAssistantMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionMessageToolCallUnionParam,
    ChatCompletionMessageFunctionToolCallParam,
)
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.completion import Completion, Completer

from mycode.renderer import (
    _get_renderer,
    set_render_style,
    render_terminal,
    render_replay,
    _prompt_fragments,
)

# APP_HOME_DIR 和 历史记录文件
APP_HOME_DIR = __import__('pathlib').Path(os.getenv('MYCODE_HOME_DIR', os.path.expanduser('~/.mycode')))
APP_HOME_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = APP_HOME_DIR / 'history.txt'

# 系统提示词
_BASE_SYSTEM_PROMPT = f"你是编程智能体 mycode。当前在 {os.getcwd()}。使用工具完成任务。直接做勿解释。"
_ADDITIONAL = os.getenv('ADDITIONAL_SYSTEM_PROMPT')
SYSTEM_PROMPT = _BASE_SYSTEM_PROMPT + (("\n" + _ADDITIONAL) if _ADDITIONAL else "")

# ---------------------------------------------------------------------------
# 429 限流自动重试
# ---------------------------------------------------------------------------
# 环境变量 E429_WAIT_SECONDS：逗号分隔的正整数秒数列表（如 "1,2,5,10"）。
# 默认（未设置 / 为空）不开启自动重试：429 直接向上抛出；
# 解析不到合法整数列表、或连续 429 次数超出列表长度时同样向上抛出。


def _parse_e429_wait_seconds(raw: str | None) -> list[int] | None:
    """解析 E429_WAIT_SECONDS 为正整数秒列表。

    - 空 / 未设置：返回 ``None``，表示不启用（429 向上抛出）；
    - 任一项非法（非正整数数字 / 空段）：返回 ``None``，表示不启用；
    - 全部合法：返回解析后的 int 列表。
    """
    if raw is None or raw.strip() == "":
        return None
    values: list[int] = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            return None
        try:
            v = int(part)
        except ValueError:
            return None
        if v <= 0:
            return None
        values.append(v)
    return values


# 模块级解析一次：429 连续发生第 n 次时取列表第 n 个值（索引 n-1）。
# 为 ``None`` 时表示不开启自动重试。
_e429_wait_list: list[int] | None = _parse_e429_wait_seconds(os.getenv('E429_WAIT_SECONDS'))

# ---------------------------------------------------------------------------
# 导入工具
# ---------------------------------------------------------------------------
from mycode.tools_registry import ToolsRegistry

client = OpenAI(
    api_key=os.getenv('API_KEY'),
    base_url=os.getenv('BASE_URL'),
)

# ===================================================================
# 从 session 导入 ADT 类型
# ===================================================================

from mycode.session import (
    UserMessage,
    AssistantMessage,
    ToolCallEvent,
    ToolResultEvent,
    InterruptEvent,
    ExceptionEvent,
    ModeChangeEvent,
    NoticeEvent,
    AgentMessage,
    SessionHistory,
)
from mycode.mode import (
    MODE_STATE,
    Mode,
    ToolCategory,
    classify_tool,
    needs_confirmation,
)
from mycode.confirm import (
    confirm_tool,
    ConfirmAction,
    format_reject,
    format_reject_no_reason,
    format_cancel,
)


# ===================================================================
# 事件总线
# ===================================================================

Handler = Callable[[AgentMessage], None]


class AgentEventBus:
    def __init__(self, session_hist: SessionHistory | None = None) -> None:
        self._handlers: list[Handler] = []
        self._session_hist = session_hist

    def register(self, handler: Handler) -> Handler:
        self._handlers.append(handler)
        return handler

    def dispatch(self, msg: AgentMessage) -> None:
        if self._session_hist is not None:
            self._session_hist.inject_meta(msg)
        for handler in self._handlers:
            handler(msg)


# ===================================================================
# 处理器 —— 持久化
# ===================================================================


def make_persist_handler(session_hist: SessionHistory) -> Handler:
    """持久化处理器工厂：闭包捕获 session_hist"""
    def persist(msg: AgentMessage) -> None:
        session_hist.append(msg)
    return persist


# ===================================================================
# replay handlers —— 同步工具状态
# ===================================================================

def make_replay_todo_sync_handler() -> Handler:
    """replay 时把 todo_write 的 ToolCallEvent 实时同步到 _todo_state。

    这样后续 dispatch 的对应 ToolResultEvent 渲染时，能看到调用
    "那一刻"的待办列表，而不是历史最终态。
    """
    from mycode.tools.todo_write import todo_write as todo_write_func

    def sync(msg: AgentMessage) -> None:
        if not isinstance(msg, ToolCallEvent):
            return
        tool_call = getattr(msg, "tool_call", None)
        if not isinstance(tool_call, dict):
            return
        func = tool_call.get("function")
        if not isinstance(func, dict) or func.get("name") != "todo_write":
            return
        try:
            args = json.loads(func.get("arguments", "") or "{}")
        except (json.JSONDecodeError, TypeError):
            return
        items = args.get("items") if isinstance(args, dict) else None
        if not isinstance(items, list):
            return
        todo_write_func(items)

    return sync


# ===================================================================
# replay_history —— 遍历 entries → dispatch render_replay
# ===================================================================

def replay_history(session_hist: SessionHistory) -> None:
    """重放历史会话"""
    bus_replay = AgentEventBus()
    # bus 按注册顺序 dispatch：render 先于 sync。
    # ToolCallEvent 派发时 render 会打印"调用工具"提示，紧接着 sync
    # 写入 _todo_state；同一 entry 处理完才到下一个，因此对应的
    # ToolResultEvent 渲染时已经看到最新状态。
    bus_replay.register(render_replay)
    bus_replay.register(make_replay_todo_sync_handler())

    for entry in session_hist.entries:
        bus_replay.dispatch(entry)


# ===================================================================
# 智能体自循环
# ===================================================================

class _AbortLoop(BaseException):
    """内部异常：用户取消 / 无理由拒绝工具调用时跳出 agent 循环。

    ``tool_result`` 为要写入 tool 消息的结果文本（区分取消与无理由拒绝）。
    """
    def __init__(self, tool_result: str) -> None:
        super().__init__(tool_result)
        self.tool_result = tool_result


def _run_tool_with_permission(
    func_name: str,
    args: dict,
    handler: Callable,
    model: str = "",
    bus: AgentEventBus | None = None,
    messages: list[ChatCompletionMessageParam] | None = None,
) -> str:
    """按模式与操作分类决定工具是否执行，返回工具结果文本。

    危险操作一律拒绝；需确认的操作弹出确认界面，按用户选择执行 / 拒绝 /
    编辑 / 取消（取消与无理由拒绝通过 ``_AbortLoop`` 抛出以跳出 agent 循环）。

    编辑命令时：与陈旧提醒一样先分发 ``NoticeEvent``（渲染 + 持久化），
    再经 ``to_user_msg()`` 注入 ``messages``，让终端与模型都能看到命令被
    用户修改；命令无变化时直接继续执行。
    """
    category = classify_tool(func_name, args)

    # 所有模式对【危险】操作一律拒绝
    if category == ToolCategory.DANGEROUS:
        return "Error: 拒绝执行危险命令"

    # 无需确认：直接执行
    if not needs_confirmation(MODE_STATE.get(), category):
        return handler(**args)

    # 需确认：弹出确认界面
    command = args.get("command") if isinstance(args, dict) else None
    action, extra = confirm_tool(func_name, category, command)

    match action:
        case ConfirmAction.REJECT_NO_REASON:
            # 无理由拒绝 → 跳出 agent 循环
            raise _AbortLoop(format_reject_no_reason())
        case ConfirmAction.CANCEL:
            # 取消 → 跳出 agent 循环
            raise _AbortLoop(format_cancel())
        case ConfirmAction.REJECT:
            return format_reject(extra or "")
        case ConfirmAction.EDIT:
            # 仅 bash：替换命令后重新分类并执行
            edited_args = dict(args)
            edited_args["command"] = extra
            # 命令有变化：先分发提醒事件（渲染 + 持久化），并注入模型，
            # 让终端与模型都能看到命令被用户修改。
            # 展示文案与 LLM 文案不同：提醒文本分别用提醒样式渲染 /
            # 包 <notice> 标签，附加内容代码块照常输出。
            if extra != command:
                new_cmd = cast(str, extra)
                event = NoticeEvent(
                    model=model,
                    notice={
                        "tag_name": "notice",
                        "content": "用户将命令修改为：",
                        "display_content": "命令修改为：",
                        "additional_content": f"```bash\n{new_cmd.rstrip(chr(0x0A))}\n```",
                    },
                )
                if bus is not None:
                    bus.dispatch(event)
                if messages is not None:
                    messages.append(event.to_user_msg())
            # 编辑后的命令若是危险命令，一律拒绝
            if classify_tool(func_name, edited_args) == ToolCategory.DANGEROUS:
                return "Error: 拒绝执行危险命令"
            # 执行编辑后的命令
            return handler(**edited_args)
        case _:  # APPROVE
            return handler(**args)


def _countdown_retry(wait: int, width: int = 1) -> None:
    """429 限流等待倒计时：红棕色显示「限流重试... n」，n 右对齐固定宽度原位跳动。

    width 取等待时间列表最大值位数，落到个位数时高位的空格覆盖旧字符，
    避免残留（如 10→9 只覆盖 1 而留下 0）；倒计时结束清除整行。
    """
    import sys
    from time import sleep

    red_brown = "\x1B[38;2;165;42;42m"
    reset = "\x1B[0m"
    for remaining in range(wait, 0, -1):
        sys.stdout.write(f"\r{red_brown}限流重试... {remaining:>{width}}{reset}")
        sys.stdout.flush()
        sleep(1)
    # 倒计时到 0：\x1B[2K 清除整行（无需手动算宽度，全角/残影都不残留）
    sys.stdout.write("\r\x1B[2K")
    sys.stdout.flush()


def agent_loop(
    messages: list[ChatCompletionMessageParam],
    bus: AgentEventBus,
    model: str,
) -> None:
    from mycode.tools.todo_write import (
        bump_stale_rounds,
        should_remind_stale_todo,
        format_stale_reminder,
        reset_stale_rounds as reset_todo_stale,
    )
    tools = ToolsRegistry.get_tools()
    # 连续 429 计数：成功产生模型事件后重置，决定取 E429_WAIT_SECONDS 第几个值
    consecutive_429 = 0
    while True:
        # ---- 陈旧待办提醒 ----
        # 每产生一个 assistant 消息前自增 stale；超过阈值且存在未完成
        # 待办时先派发 NoticeEvent（让终端显示 + 持久化，与 replay 渲染
        # 一致），再通过 to_user_msg() 注入 messages 供模型看到，然后清零
        # stale（避免连续打扰）。
        bump_stale_rounds()
        if should_remind_stale_todo():
            reminder_text = format_stale_reminder()
            # 先分发事件：走 bus 渲染 + 持久化
            # （用 NoticeEvent 而非 UserMessage，避免按用户消息格式再输出一遍）
            # 陈旧提醒使用 tag_name="reminder"，经 to_user_msg() 生成
            # <reminder>...</reminder> 标签。
            event = NoticeEvent(
                model=model,
                notice={"tag_name": "reminder", "content": reminder_text},
            )
            bus.dispatch(event)
            # 再发给模型：通过 to_user_msg() 转成用户消息并带 <reminder> 标签
            messages.append(event.to_user_msg())
            reset_todo_stale()

        # 调用模型
        try:
            # noinspection PyTypeChecker
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
        except KeyboardInterrupt:
            # Ctrl-C 中断大模型等待：分发 InterruptEvent，然后跳出循环、
            # agent_loop 自然返回，由外层 continue 接下一轮输入。
            bus.dispatch(InterruptEvent(model=model, interrupt={"abort": False}))
            return
        except RateLimitError:
            # 429 限流：按连续发生次数取 E429_WAIT_SECONDS 中的秒数，
            # 倒计时等待后静默重试，不跳出 agent 循环。
            consecutive_429 += 1
            wait_list = _e429_wait_list
            if wait_list is None or consecutive_429 > len(wait_list):
                # 配置缺失 / 连续次数超出配置列表长度：功能不启用，向上抛出
                raise
            wait = wait_list[consecutive_429 - 1]
            # 数字右对齐：宽度取等待时间列表最大值位数（如 "1,2,5,10" → 2），
            # 倒计时从 10→9 递减时高位被空格覆盖，避免残留旧数字。
            try:
                _countdown_retry(wait, width=len(str(max(wait_list))))
            except KeyboardInterrupt:
                # Ctrl-C 中断 429 倒计时等待：分发 InterruptEvent 后跳出
                # agent 循环（不继续重试）。
                bus.dispatch(InterruptEvent(model=model, interrupt={"abort": False}))
                return
            continue
        # 成功产生模型事件：重置连续 429 计数
        consecutive_429 = 0

        # noinspection PyUnresolvedReferences
        choice = response.choices[0]
        message = choice.message
        content = message.content

        # 将 Pydantic tool_calls 转为 list[ChatCompletionMessageToolCallUnionParam]
        serialized_tool_calls = cast(
            list[ChatCompletionMessageToolCallUnionParam],
            [tc.model_dump() for tc in (message.tool_calls or [])]
        )

        # 消息列表追加模型回复。
        # 某些模型供应商不接受 tool_calls 字段为空数组（会报 invalid_parameter_error），
        # 因此仅当确有 tool_calls 时才设置该字段。
        assistant_msg: ChatCompletionAssistantMessageParam = {
            'role': 'assistant', 'content': content
        }
        if serialized_tool_calls:
            assistant_msg['tool_calls'] = serialized_tool_calls
        messages.append(assistant_msg)

        # dispatch AI 回复
        bus.dispatch(AssistantMessage(model=model, message=assistant_msg))

        # 非工具调用结束循环
        if choice.finish_reason != 'tool_calls':
            return

        # 收集所有 function 类型的 tool_call（按 assistant 返回顺序）。
        # 后续无论工具是被异常打断还是正常完成，都能精准定位剩余未处理的项，
        # 以便为它们补占位 tool 消息，避免 assistant.tool_calls 出现"孤儿子项"。
        pending: list[ChatCompletionMessageFunctionToolCallParam] = [
            cast(ChatCompletionMessageFunctionToolCallParam, tc)
            for tc in serialized_tool_calls
            if tc.get("type") == "function"
        ]

        # 中断/异常原因：正常路径保持 None；赋值后仅用于 isinstance 判断与
        # 构造 skip_message，不再向上抛（agent_loop 自身处理完毕即返回）。
        cause: BaseException | None = None
        # 记录异常发生在 pending 中的索引位置（0-based），用于精确补齐剩余项。
        # 异常项本身已在 finally 中补上 tool 消息；只需为「此索引之后」的
        # 尚未开始执行的 tool_call 补占位消息，已正常完成的更早项不动。
        interrupted_at: int = -1

        for idx, tool_call in enumerate(pending):
            func_name = tool_call["function"]["name"]
            handler = ToolsRegistry.get_handler(func_name)

            # dispatch 工具调用事件
            bus.dispatch(ToolCallEvent(model=model, tool_call=tool_call))

            args_str = tool_call["function"]["arguments"]
            tool_result: str | None = None
            try:
                # 解析工具参数（权限检查与执行在 _run_tool_with_permission 内）
                # noinspection PyUnresolvedReferences
                args_ = json.loads(args_str)
                if handler is None:
                    tool_result = f"Error: 未知工具 '{func_name}'"
                else:
                    tool_result = _run_tool_with_permission(
                        func_name,
                        args_ if isinstance(args_, dict) else {},
                        handler,
                        model=model,
                        bus=bus,
                        messages=messages,
                    )
            except _AbortLoop as e:
                # 用户取消/无理由拒绝：跳出（结果文本区分两种情况）。
                # 分发 abort 标记的 InterruptEvent，replay 时不渲染 ^C。
                cause = e
                interrupted_at = idx
                tool_result = e.tool_result
                bus.dispatch(InterruptEvent(model=model, interrupt={"abort": True}))
                break
            except KeyboardInterrupt as e:
                # Ctrl-C 中断工具执行：先 dispatch InterruptEvent 让 ^C 后
                # 立即换行，然后用占位错误信息补齐 tool 消息（finally 中），
                # 最后跳出循环、agent_loop 自然返回。
                cause = e
                interrupted_at = idx
                tool_result = "Error: 工具执行被用户中断"
                bus.dispatch(InterruptEvent(model=model, interrupt={"abort": False}))
                break
            except BaseException as e:
                # 工具执行抛异常：补一条 tool 错误消息、dispatch ExceptionEvent
                # 并跳出循环。注意：traceback 必须在 except 块内获取，
                # 否则 sys.exc_info() 已被清除，format_exc() 会返回
                # "NoneType: None"。
                import traceback as _tb
                cause = e
                interrupted_at = idx
                tool_result = f"Error: 工具执行失败: {type(e).__name__}: {e}"
                bus.dispatch(ExceptionEvent(model=model, exception={
                    "type": type(e).__name__,
                    "message": str(e),
                    "traceback": _tb.format_exc().rstrip(),
                }))
                break
            finally:
                # 始终补上对应的 tool 消息与 ToolResultEvent，
                # 避免下次恢复会话时模型供应商校验失败
                # （tool call result does not follow tool call）。
                # finally 在 break 前执行，能保证补上消息后再跳出循环。
                if tool_result is None:
                    tool_result = "Error: 工具异常终止"
                tool_msg = ChatCompletionToolMessageParam(
                    role='tool', tool_call_id=tool_call["id"], content=tool_result
                )
                messages.append(tool_msg)
                bus.dispatch(ToolResultEvent(
                    model=model,
                    tool_result={
                        "tool_call_id": tool_call["id"],
                        "content": tool_result,
                        "tool_name": func_name,
                    },
                ))

        # 如果因为异常/Ctrl-C 提前跳出循环，assistant 消息里仍包含所有
        # pending 中的 tool_calls，但只到 interrupted_at（含）的项补上了
        # tool 消息。必须为「此索引之后」的剩余未处理的 tool_call 也补占位
        # tool 消息，并 dispatch 相应事件，保持消息序列对模型合法。
        if cause is not None:
            # 提示事件（InterruptEvent / ExceptionEvent）已在各 except 块内
            # dispatch —— 此处仅构造 skip_message 并补齐剩余 tool_call。
            skip_message = (
                "Error: 因用户中断操作跳过工具执行"
                if isinstance(cause, KeyboardInterrupt)
                else f"Error: 因先前错误跳过工具执行: "
                     f"{type(cause).__name__}: {cause}"
            )

            # 为「此索引之后」的剩余未处理的 tool_call 补占位 tool 消息
            # 并 dispatch 相应事件，保持消息序列对模型合法。
            for remaining in pending[interrupted_at + 1:]:
                bus.dispatch(ToolCallEvent(model=model, tool_call=remaining))
                tool_msg = ChatCompletionToolMessageParam(
                    role='tool', tool_call_id=remaining["id"], content=skip_message
                )
                messages.append(tool_msg)
                bus.dispatch(ToolResultEvent(
                    model=model,
                    tool_result={
                        "tool_call_id": remaining["id"],
                        "content": skip_message,
                        "tool_name": func_name,
                    },
                ))
            # 所有提示事件已 dispatch、补齐已完成。让 agent_loop 自然返回，
            # 由外层 main() 继续接受下一轮用户输入。
            return


# ===================================================================
# 命令处理 —— 交互命令的行为入口
# ===================================================================

def _should_show_retry_hint(entries: list[AgentMessage]) -> AgentMessage | None:
    """是否需要在接收下一轮用户输入前提示「可 Ctrl-T 或 /retry 继续」。

    实时与重放统一判定：从末尾**跳过工具结果消息**（工具被中断/异常后
    会补齐 tool 占位消息并派发 ToolResultEvent），看**最后一条非工具
    事件**是否为中断（InterruptEvent，含真实 Ctrl-C 与用户取消）或异常
    （ExceptionEvent）。正常完成的会话最后一条非工具事件是 assistant
    回复，不会误报。

    返回触发提示的事件（InterruptEvent / ExceptionEvent）或 None。
    返回事件让 main 用其 id 作为水印：仅当水印变化（新中断/异常事件
    出现）才重新渲染提示，避免 Ctrl-C 静默继续时反复渲染。
    """
    last_non_tool = next(
        (e for e in reversed(entries) if not isinstance(e, ToolResultEvent)),
        None,
    )
    if isinstance(last_non_tool, (InterruptEvent, ExceptionEvent)):
        return last_non_tool
    return None


def _render_retry_hint_once(
    entries: list[AgentMessage],
    last_hint_id: str | None,
) -> str | None:
    """根据 entries 状态渲染重试提示并返回新的水印 id；已渲染则不重复。

    传 ``last_hint_id`` 是上一次渲染过提示的事件 id（初始 None）；若当前
    触发事件 id 相同则跳过渲染（避免 prompt 阶段 Ctrl-C 静默继续时反复
    渲染同一提示）。返回更新后的水印 id（无论是否渲染，均返回当前触发
    事件 id 以便上层追踪）。
    """
    trigger = _should_show_retry_hint(entries)
    if trigger is None:
        return last_hint_id
    if trigger.id == last_hint_id:
        return last_hint_id
    _get_renderer().render_retry_hint()
    return trigger.id


def _switch_mode(model: str, bus: AgentEventBus, mode: Mode) -> None:
    """切换模式：更新公共状态并派发 ModeChangeEvent（渲染 + 持久化）。"""
    MODE_STATE.set(mode)
    bus.dispatch(ModeChangeEvent(model=model, mode=mode.value))


def _handle_retry_command(
    hist_messages: list[ChatCompletionMessageParam],
    bus: AgentEventBus,
    model: str,
) -> None:
    """/retry：重新进入 agent 循环。

    当消息列表最后一条是 user 或 tool 消息时（工具被中断/异常后，最后一条
    通常是补齐的 tool 占位消息，或尚未得到回复的用户输入），直接重发——
    消息列表已包含最后那条，无需追加新消息。

    否则（最后一条是 assistant 回复、system 或为空），追一条用户消息
    "继续"（dispatch UserMessage 渲染 + 持久化），再进入 agent 循环。
    """
    if hist_messages and hist_messages[-1].get("role") in ("user", "tool"):
        agent_loop(hist_messages, bus, model)
        return
    continue_msg = ChatCompletionUserMessageParam(role='user', content='继续')
    hist_messages.append(continue_msg)
    bus.dispatch(UserMessage(model=model, message=continue_msg))
    agent_loop(hist_messages, bus, model)


# ===================================================================
# CLI 命令补全 & 参数解析
# ===================================================================

def _prompt_user_input(session: PromptSession[str]) -> str | None:
    """读取一轮用户输入。

    default 风格的输入区上下留白由布局提前预留（见
    ``renderer.apply_input_style``：输入区根容器上下各挂 1 行灰色背景空行）。

    Ctrl-C 发生在输入过程中（``session.prompt`` 内部）时静默放弃
    本次输入并返回 ``None``——不输出任何内容，由调用方直接继续
    等待下一轮输入。EOF（Ctrl-D）则照常向上抛出。
    """
    try:
        # prompt 接受 list[tuple[str, str] | tuple[str,str,Callable]]，
        # 此处仅含二元组，用 cast 让 mypy 通过（list 不变性）。
        return session.prompt(cast(Any, _prompt_fragments()))
    except KeyboardInterrupt:
        return None


class MycCommandCompleter(Completer):
    COMMANDS = ["/q", "/quit", "/ask", "/auto", "/yolo", "/retry"]

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith('/'):
            for cmd in self.COMMANDS:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text), display=cmd)


def _create_prompt_session() -> PromptSession[str]:
    """创建 prompt_toolkit 输入会话（按渲染风格配置样式）。

    default 风格输入区灰色背景说明：
      - ``''`` 根样式：让有内容的单元格继承灰色背景；
      - ``'mycode-input'``：挂到布局根容器上，借助
        ``_Split.write_to_screen`` 下发 parent_style → 各
        ``Window._apply_style → Screen.fill_area``，把「整块输入区」
        （含空行、行尾空白以及向下延伸到终端底部的剩余空间）都填充
        为灰色背景。单靠根样式只会给已渲染的字符上色，空白行不会覆盖。
    """
    renderer = _get_renderer()

    # shift-tab 切换模式（自动 → 全权 → 询问 → 自动）
    from prompt_toolkit.key_binding import KeyBindings
    kb = KeyBindings()

    @kb.add("s-tab")
    def _cycle_mode(event):
        old = MODE_STATE.get()
        new = MODE_STATE.cycle()
        # 模式切换作为一个事件分发（交由 main 的 bus 处理）。
        # 此处仅记录待派发标志，由 main 在下一轮读取。
        event.app.exit(result="__mode_cycle__")

    @kb.add("c-t")
    def _retry(event):
        # Ctrl-T：输入阶段按 Ctrl-T 等价于输入 /retry，重新进入 agent
        # 循环继续被中断的对话。返回 /retry 文本，复用 /retry 的命令解析。
        # （冲突最小：仅覆盖 emacs transpose-chars / vi indent 低频编辑。）
        event.app.exit(result="/retry")

    session: PromptSession[str] = PromptSession(
        history=FileHistory(HISTORY_FILE),
        completer=MycCommandCompleter(),
        multiline=True,
        style=renderer.create_prompt_style(),
        prompt_continuation='',
        erase_when_done=True,
        key_bindings=kb,
        # 输入框为空时灰显占位文字「↵ 换行，Alt-↵（ESC ↵）发送」
        placeholder=renderer.input_placeholder(),
    )
    renderer.apply_input_style(session)
    return session


def parse_args():
    parser = argparse.ArgumentParser(prog='mycode', description='mycode - 编程智能体')
    parser.add_argument('-r', '--resume', type=str, metavar='SESSION_ID',
                        help='恢复指定会话')
    parser.add_argument('-c', '--continue', dest='continue_session', action='store_true',
                        help='恢复当前目录的最新会话')
    parser.add_argument('-s', '--style', dest='style', default='default',
                        choices=('default', 'classic'), metavar='{default,classic}',
                        help='渲染风格：default（默认）或 classic')
    # 将 -h/--help 的 help 文本改为中文
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            action.help = '显示此帮助信息并退出'
    return parser.parse_args()


# ===================================================================
# main
# ===================================================================

from mycode.session import find_latest_session_file, get_session_file

def main():
    args = parse_args()

    # 设置全局渲染风格
    set_render_style(args.style)

    model = os.getenv('MODEL_NAME') or ''

    # 消息列表初始化系统提示词
    hist_messages: list[ChatCompletionMessageParam] = [
        ChatCompletionSystemMessageParam(role='system', content=SYSTEM_PROMPT),
    ]

    session_hist: SessionHistory

    if args.resume:
        session_id = args.resume
        session_file = get_session_file(session_id)
        if session_file is None:
            print(f"未找到会话 ID 为 {session_id} 的会话。")
            sys.exit(1)

        session_hist = SessionHistory.load(session_file)
        hist_messages.extend(session_hist.get_messages())
    elif args.continue_session:
        session_file = find_latest_session_file()
        if session_file is None:
            print(f"未找到当前目录的会话记录。")
            sys.exit(1)

        session_hist = SessionHistory.load(session_file)
        hist_messages.extend(session_hist.get_messages())
    else:
        session_hist = SessionHistory(cwd=os.getcwd(), model=model)

    # 恢复会话时同步模式（session 公共字段），新会话默认自动
    MODE_STATE.set(session_hist.mode)

    # ---- 组装事件总线（CLI 场景：持久化 + 终端渲染） ----
    bus = AgentEventBus(session_hist=session_hist)
    bus.register(make_persist_handler(session_hist))
    bus.register(render_terminal)

    # 输出开头信息（标题橙色加粗）
    print(f"\x1B[38;2;255;165;0;1m【mycode】\x1B[0m")
    print(f"会话 ID: {session_hist.session_uuid}")
    print()

    if args.resume or args.continue_session:
        replay_history(session_hist)

    # ---- prompt_toolkit 配置 ----

    session = _create_prompt_session()

    # 已渲染过提示的「触发事件 id」水印：仅当新的中断/异常事件出现时才
    # 重新渲染提示，避免 prompt 阶段 Ctrl-C 静默继续时反复渲染同一提示。
    # 实际去重逻辑在 _render_retry_hint_once；首次 last_hint_id 为 None
    # 保证重放/实时首次走到这里时一定会渲染一次提示。
    last_hint_id: str | None = None
    while True:
        # 每次接收输入前检测：最近的非工具事件若是中断/异常，提示可继续
        # （Ctrl-T 或 /retry）。重放结束、实时一轮 agent_loop 结束后都会
        # 走到这里，因此实时与重放共用同一判定，逻辑统一。
        last_hint_id = _render_retry_hint_once(session_hist.entries, last_hint_id)
        try:
            user_input = _prompt_user_input(session)
            if user_input is None:
                # Ctrl-C 发生在输入过程中：不输出任何内容，直接继续
                continue

            stripped = user_input.strip()

            # ---- shift-tab 循环切换模式 ----
            if stripped == "__mode_cycle__":
                _switch_mode(model, bus, MODE_STATE.get())
                continue

            # ---- 模式切换命令 ----
            if stripped in {"/ask", "/auto", "/yolo"}:
                _switch_mode(model, bus, {
                    "/ask": Mode.ASK,
                    "/auto": Mode.AUTO,
                    "/yolo": Mode.YOLO,
                }[stripped])
                continue

            if stripped in {"/q", "/quit"}:
                break

            # ---- /retry：中断/异常后恢复 agent 循环 ----
            if stripped == "/retry":
                _handle_retry_command(hist_messages, bus, model)
                continue

            # 消息列表追加用户输入并进入智能体循环
            user_msg = ChatCompletionUserMessageParam(role='user', content=user_input)
            hist_messages.append(user_msg)

            # dispatch 用户消息（持久化 + 渲染）
            bus.dispatch(UserMessage(model=model, message=user_msg))

            agent_loop(hist_messages, bus, model)

        except EOFError:
            # Ctrl-D: 退出程序
            break

        except KeyboardInterrupt:
            # Ctrl-C 在 prompt 阶段被触发：agent_loop 未运行，相当于什么都没
            # 中断；输出换行让光标回到新行，再 continue 重新接受下一轮输入。
            print()
            continue

        except Exception as e:
            # 外层自身产生的异常（如 session.prompt / 消息解析等）；
            # agent_loop 内部的异常已由它自身处理。
            import traceback as tb
            tb_lines = tb.format_exc().rstrip()
            bus.dispatch(ExceptionEvent(model=model, exception={
                "type": type(e).__name__,
                "message": str(e),
                "traceback": tb_lines,
            }))
            continue

    # ---- 退出提示 ----

    entry_count = len([e for e in session_hist.entries if isinstance(e, (UserMessage, AssistantMessage, ToolResultEvent))])
    if entry_count == 0:
        # 没有任何用户消息，删除只有 session 记录的文件
        session_hist.file_path.unlink(missing_ok=True)
        print("\n无输入，会话已清理。")
    else:
        # 退出提示（恢复命令按风格渲染）
        _get_renderer().render_resume_hint(f"{sys.argv[0]} -r {session_hist.session_uuid}")

if __name__ == '__main__':
    main()
