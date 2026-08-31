"""
会话历史记录模块
"""

from __future__ import annotations

import os
import uuid
import json
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import TypedDict, Optional, List, Dict, Any, cast, Protocol, NoReturn

from openai.types.chat import ChatCompletionMessageParam
from openai.types.chat import ChatCompletionUserMessageParam
from openai.types.chat import ChatCompletionAssistantMessageParam
from openai.types.chat import ChatCompletionToolMessageParam
from openai.types.chat import ChatCompletionMessageFunctionToolCallParam

from mycode.mode import Mode, MODE_STATE


# 应用目录
APP_HOME_DIR = Path(os.getenv('MYCODE_HOME_DIR', os.path.expanduser('~/.mycode')))
SESSIONS_DIR = APP_HOME_DIR / "sessions"


class SessionData(TypedDict):
    """会话元数据"""
    id: str
    cwd: str
    mode: str


class ExceptionData(TypedDict):
    """异常自定义数据"""
    type: str
    message: str
    traceback: str


class InterruptData(TypedDict):
    """中断数据：abort 标记取消/无理由拒绝（区别于真实 Ctrl-C）。"""
    abort: bool


class NoticeData(TypedDict, total=False):
    """提醒数据：tag_name 为标签名，content 为发给 LLM 的提醒文本，
    display_content 为展示渲染用的提醒（非空时优先于 content），
    additional_content 为附加内容（如代码块）。
    """
    content: str
    display_content: str
    additional_content: str
    tag_name: str


class ToolResultData(TypedDict):
    """工具结果数据：tool_call_id 关联对应的工具调用，
    content 为结果文本，tool_name 为工具名。
    """
    tool_call_id: str
    content: str
    tool_name: str


def sanitize_path(path: str) -> str:
    r"""将路径中的元字符替换为减号：/ : ? * " < > |（路径分隔符、Windows盘符分隔符及非法文件名字符）"""
    if not path:
        raise ValueError("路径不能为空")
    special_chars = ["/", "\\", ":", "?", "*", '"', "<", ">", "|"]
    result = path
    for char in special_chars:
        result = result.replace(char, "-")
    return result


def get_iso_timestamp() -> str:
    """获取ISO8601带时区格式的微秒级时间戳"""
    return datetime.now().astimezone().isoformat()


def find_latest_session_file() -> Path | None:
    """根据当前目录找到最新的会话文件（按修改时间排序）"""
    sanitized_cwd = sanitize_path(os.getcwd())
    session_dir = SESSIONS_DIR / sanitized_cwd
    if not session_dir.exists():
        return None
    jsonl_files = sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonl_files[0] if jsonl_files else None


def get_session_file(session_id_: str) -> Path | None:
    """根据 session id（完整UUID）获取会话文件路径"""
    sanitized_cwd = sanitize_path(os.getcwd())
    target = SESSIONS_DIR / sanitized_cwd / f"{session_id_}.jsonl"
    return target if target.exists() else None


def _get_dirs_file(dir_path: str) -> Path:
    """获取目录信任记录文件路径。

    返回 SESSIONS_DIR / sanitized_cwd / ".dirs"。
    """
    sanitized_cwd = sanitize_path(dir_path)
    return SESSIONS_DIR / sanitized_cwd / ".dirs"


def is_dir_trusted(dir_path: str) -> bool:
    """检查指定目录是否已被信任。

    在 .dirs 文件中查找该目录的绝对路径。
    """
    dirs_file = _get_dirs_file(dir_path)
    if not dirs_file.exists():
        return False
    abs_path = os.path.abspath(dir_path)
    with open(dirs_file, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip() == abs_path:
                return True
    return False


def trust_dir(dir_path: str) -> None:
    """将目录标记为已信任。

    将目录的绝对路径追加到 .dirs 文件，每行一个。
    """
    dirs_file = _get_dirs_file(dir_path)
    dirs_file.parent.mkdir(parents=True, exist_ok=True)
    abs_path = os.path.abspath(dir_path)
    # 避免重复写入
    if is_dir_trusted(dir_path):
        return
    with open(dirs_file, 'a', encoding='utf-8') as f:
        f.write(abs_path + '\n')


# ===================================================================
# Protocol & ADT —— 消息/动作类型
# ===================================================================

class MessageProtocol(Protocol):
    id: str
    parent_id: Optional[str]
    model: str
    entry_type: str
    time: str
    mode: str


@dataclass
class SessionRecord(MessageProtocol):
    """session 记录，仅用于文件标识"""
    session: SessionData
    model: str
    mode: str = Mode.AUTO.value
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "session"
    time: str = ""


@dataclass
class UserMessage(MessageProtocol):
    model: str
    message: ChatCompletionUserMessageParam
    mode: str = Mode.AUTO.value
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "message"
    time: str = ""


@dataclass
class AssistantMessage(MessageProtocol):
    model: str
    message: ChatCompletionAssistantMessageParam
    mode: str = Mode.AUTO.value
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "message"
    time: str = ""


@dataclass
class ToolCallEvent(MessageProtocol):
    model: str
    tool_call: ChatCompletionMessageFunctionToolCallParam
    mode: str = Mode.AUTO.value
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "tool_call"
    time: str = ""


@dataclass
class ToolResultEvent(MessageProtocol):
    """工具执行结果。"""
    model: str
    tool_result: ToolResultData
    mode: str = Mode.AUTO.value
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "tool_result"
    time: str = ""

    def to_tool_msg(self) -> ChatCompletionToolMessageParam:
        """转为 ``ChatCompletionToolMessageParam``（喂给模型）。"""
        return ChatCompletionToolMessageParam(
            role='tool',
            tool_call_id=self.tool_result["tool_call_id"],
            content=self.tool_result["content"],
        )


@dataclass
class InterruptEvent(MessageProtocol):
    """中断事件。"""
    model: str
    interrupt: InterruptData
    mode: str = Mode.AUTO.value
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "interrupt"
    time: str = ""



@dataclass
class ExceptionEvent(MessageProtocol):
    model: str
    exception: ExceptionData
    mode: str = Mode.AUTO.value
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "exception"
    time: str = ""


@dataclass
class ModeChangeEvent(MessageProtocol):
    """模式切换事件（session 公共字段记录）。"""
    model: str
    mode: str
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "mode_change"
    time: str = ""


class AbortLoop(BaseException):
    """内部异常：用户取消 / 无理由拒绝 / ask_user 用户中止时跳出 agent 循环。

    ``tool_result`` 为要写入 tool 消息的结果文本（区分取消与无理由拒绝等）。
    """

    def __init__(self, tool_result: str) -> None:
        super().__init__(tool_result)
        self.tool_result = tool_result


@dataclass
class NoticeEvent(MessageProtocol):
    """系统注入的提醒（如陈旧待办提醒、命令已更新提醒）。"""
    model: str
    notice: NoticeData
    mode: str = Mode.AUTO.value
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "notice"
    time: str = ""

    def to_user_msg(self) -> ChatCompletionUserMessageParam:
        """转为 ``ChatCompletionUserMessageParam``（喂给模型）。

        ``notice.content`` 整体用 ``<tag_name>`` 标签包裹后喂给模型；
        ``notice.additional_content``，如代码块，照常输出在标签之后。
        """
        tag = self.notice["tag_name"]
        content = self.notice.get("content", "")
        additional = self.notice.get("additional_content", "")
        if additional:
            content_full = (
                f"<{tag}>{content}</{tag}>\n"
                f"{additional.rstrip(chr(0x0A))}"
            )
        else:
            content_full = f"<{tag}>{content}</{tag}>"
        return ChatCompletionUserMessageParam(role='user', content=content_full)


AgentMessage = (
    SessionRecord
    | UserMessage
    | AssistantMessage
    | ToolCallEvent
    | ToolResultEvent
    | InterruptEvent
    | ExceptionEvent
    | ModeChangeEvent
    | NoticeEvent
)


def assert_never(arg: NoReturn) -> NoReturn:
    raise AssertionError(f"未处理的消息类型: {type(arg).__name__}")


def _msg_to_dict(msg: AgentMessage) -> Dict[str, Any]:
    """将 AgentMessage 转为 JSONL 字典。

    序列化规范：只有 ``MessageProtocol`` 定义的公共字段
    （time / type / id / parent_id / model / mode）平铺在顶层；
    各事件自己的扩展字段聚合在以 ``entry_type`` 为名的 key 下
    （如 ``notice`` / ``interrupt`` / ``exception``），
    与 ``type`` 值保持一致。
    """
    d: Dict[str, Any] = {
        "time": msg.time,
        "type": msg.entry_type,
        "id": msg.id,
        "parent_id": msg.parent_id,
        "model": msg.model,
        "mode": msg.mode,
    }
    match msg:
        case SessionRecord(session=session_data):
            d["session"] = session_data
        case UserMessage(message=message):
            d["message"] = message
        case AssistantMessage(message=message):
            d["message"] = message
        case ToolCallEvent(tool_call=tool_call):
            d["tool_call"] = tool_call
        case ToolResultEvent(tool_result=tool_result):
            d["tool_result"] = tool_result
        case InterruptEvent(interrupt=interrupt):
            d["interrupt"] = interrupt
        case ExceptionEvent(exception=exc_data):
            d["exception"] = exc_data
        case ModeChangeEvent():
            pass  # mode 已由 base dict 记录
        case NoticeEvent(notice=notice):
            d["notice"] = notice
        case _ as unreachable:
            assert_never(unreachable)
    return d


def _dict_to_agent_message(data: Dict[str, Any]) -> AgentMessage | None:
    """将 JSONL 字典转为 AgentMessage。

    反序列化时从事务扩展字段读取，结构见 ``_msg_to_dict``。
    """
    entry_type = data.get("type")
    if entry_type not in ("message", "tool_call", "tool_result", "interrupt", "session", "exception", "notice", "mode_change"):
        raise ValueError(f"未知的条目类型: {entry_type}")
    base_kwargs = {
        "id": data["id"],
        "parent_id": data.get("parent_id"),
        "model": data["model"],
        "time": data["time"],
        "mode": data.get("mode", Mode.AUTO.value),
    }
    if entry_type == "session":
        return SessionRecord(session=cast(SessionData, data["session"]), **base_kwargs)
    elif entry_type == "message":
        msg = data.get("message")
        role = msg.get("role") if msg else None
        if role == "user":
            return UserMessage(message=cast(ChatCompletionUserMessageParam, msg), **base_kwargs)
        elif role == "assistant":
            return AssistantMessage(message=cast(ChatCompletionAssistantMessageParam, msg), **base_kwargs)
    elif entry_type == "tool_call":
        tc_data = data.get("tool_call")
        if not isinstance(tc_data, dict) or tc_data.get("type") != "function":
            raise ValueError(f"无效的 tool_call 数据: {tc_data}")
        return ToolCallEvent(
            tool_call=cast(ChatCompletionMessageFunctionToolCallParam, cast(Any, tc_data)),
            **base_kwargs,
        )
    elif entry_type == "tool_result":
        tr = data.get("tool_result")
        if not isinstance(tr, dict):
            raise ValueError(f"无效的 tool_result 数据: {data}")
        return ToolResultEvent(
            tool_result=cast(ToolResultData, tr),
            **base_kwargs,
        )
    elif entry_type == "interrupt":
        return InterruptEvent(
            interrupt=cast(InterruptData, data.get("interrupt", {"abort": False})),
            **base_kwargs,
        )
    elif entry_type == "exception":
        exc_data = data.get("exception", {})
        return ExceptionEvent(exception=cast(ExceptionData, exc_data), **base_kwargs)
    elif entry_type == "mode_change":
        return ModeChangeEvent(**base_kwargs)
    elif entry_type == "notice":
        nt = data.get("notice")
        if not isinstance(nt, dict) or not nt.get("tag_name"):
            raise ValueError(f"无效的 notice 数据：缺少 tag_name ({data})")
        return NoticeEvent(
            notice=cast(NoticeData, nt),
            **base_kwargs,
        )
    # unreachable
    raise ValueError(f"未知的条目类型: {entry_type}")


class SessionHistory:
    """会话历史记录管理器"""

    def __init__(self, cwd: str, model: str):
        self.cwd = cwd
        self.model = model
        self.mode: Mode = Mode.AUTO
        sanitized_cwd = sanitize_path(cwd)
        self.directory = SESSIONS_DIR / sanitized_cwd
        self.directory.mkdir(parents=True, exist_ok=True)

        # 生成完整UUID，用作文件名和session结构体的id
        self.session_uuid = str(uuid.uuid4())

        self.file_name = f"{self.session_uuid}.jsonl"
        self.file_path = self.directory / self.file_name

        self.entries: List[AgentMessage] = []

        # 写入初始 session 记录
        session_data = SessionData(id=self.session_uuid, cwd=cwd, mode=Mode.AUTO.value)
        session_record = SessionRecord(
            session=session_data,
            model=model,
            id=self.session_uuid[:8],
            parent_id=None,
            entry_type="session",
            time=get_iso_timestamp(),
        )
        self.append(session_record)

    def _append_to_file(self, data: Dict[str, Any]) -> None:
        """追加一行JSON到文件"""
        with open(self.file_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(data, ensure_ascii=False) + '\n')

    def _next_id(self) -> str:
        """生成不冲突的记录id"""
        full_uuid = str(uuid.uuid4())
        short_id = full_uuid[:8]
        existing_ids = {e.id for e in self.entries}
        return short_id if short_id not in existing_ids else full_uuid

    def inject_meta(self, msg: AgentMessage) -> None:
        """注入 id、parent_id、time、mode 元数据"""
        msg.id = self._next_id()
        msg.parent_id = self.entries[-1].id if self.entries else None
        msg.time = get_iso_timestamp()
        msg.mode = MODE_STATE.get().value

    def append(self, msg: AgentMessage) -> None:
        """追加一条 AgentMessage（元数据需在调用前注入好）"""
        self._append_to_file(_msg_to_dict(msg))
        self.entries.append(msg)

    @classmethod
    def load(cls, file_path: Path) -> SessionHistory:
        """从JSONL文件加载会话历史"""
        instance = cls.__new__(cls)
        instance.file_path = file_path
        instance.session_uuid = ''
        instance.file_name = file_path.name
        instance.entries = []

        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    entry_type = data.get("type")
                    # 第一条必须是 session 记录
                    if entry_type == "session":
                        session_data = cast(SessionData, data["session"])
                        instance.session_uuid = session_data["id"]
                        instance.cwd = session_data.get("cwd", os.getcwd())
                        instance.directory = file_path.parent
                        instance.model = data["model"]
                        instance.mode = Mode(session_data.get("mode", Mode.AUTO.value))
                    # 所有类型都加入 entries
                    agent_msg = _dict_to_agent_message(data)
                    if agent_msg is not None:
                        instance.entries.append(agent_msg)

        # 恢复模式：取最后一条消息自带的模式（覆盖 session 记录初值）
        if instance.entries:
            last = instance.entries[-1]
            mode_val = getattr(last, "mode", None)
            if mode_val:
                try:
                    instance.mode = Mode(mode_val)
                except ValueError:
                    pass

        if not instance.entries:
            raise ValueError(f"无法加载空会话文件: {file_path}")
        first = instance.entries[0]
        if not isinstance(first, SessionRecord):
            raise ValueError(f"无效的会话文件：第一条记录不是session记录: {file_path}")

        return instance

    def get_messages(self) -> List[ChatCompletionMessageParam]:
        """获取发送给模型的消息列表。

        ``NoticeEvent`` 经 ``to_user_msg()``、``ToolResultEvent`` 经
        ``to_tool_msg()`` 转成对应 ``ChatCompletion...Param`` 加入列表，
        确保会话恢复后提醒与工具结果仍能进入模型上下文。
        """
        result: List[ChatCompletionMessageParam] = []
        for e in self.entries:
            if isinstance(e, (UserMessage, AssistantMessage)):
                result.append(e.message)
            elif isinstance(e, ToolResultEvent):
                result.append(e.to_tool_msg())
            elif isinstance(e, NoticeEvent):
                result.append(e.to_user_msg())
        return result
