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


# 应用目录
APP_HOME_DIR = Path(os.getenv('MYCODE_HOME_DIR', os.path.expanduser('~/.mycode')))
SESSIONS_DIR = APP_HOME_DIR / "sessions"


class SessionData(TypedDict):
    """会话元数据"""
    id: str
    cwd: str


class ExceptionData(TypedDict):
    """异常自定义数据"""
    type: str
    message: str
    traceback: str


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


# ===================================================================
# Protocol & ADT —— 消息/动作类型
# ===================================================================

class MessageProtocol(Protocol):
    id: str
    parent_id: Optional[str]
    model: str
    entry_type: str
    time: str


@dataclass
class SessionRecord(MessageProtocol):
    """session 记录，仅用于文件标识"""
    session: SessionData
    model: str
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "session"
    time: str = ""


@dataclass
class UserMessage(MessageProtocol):
    model: str
    message: ChatCompletionUserMessageParam
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "message"
    time: str = ""


@dataclass
class AssistantMessage(MessageProtocol):
    model: str
    message: ChatCompletionAssistantMessageParam
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "message"
    time: str = ""


@dataclass
class ToolCallEvent(MessageProtocol):
    model: str
    tool_call: ChatCompletionMessageFunctionToolCallParam
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "tool_call"
    time: str = ""


@dataclass
class ToolResultEvent(MessageProtocol):
    model: str
    message: ChatCompletionToolMessageParam
    tool_name: str = ""
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "message"
    time: str = ""


@dataclass
class InterruptEvent(MessageProtocol):
    model: str
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "interrupt"
    time: str = ""



@dataclass
class ExceptionEvent(MessageProtocol):
    model: str
    exception: ExceptionData
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "exception"
    time: str = ""


@dataclass
class ReminderEvent(MessageProtocol):
    """系统注入的提醒（如陈旧待办提醒）。

    与 ``UserMessage`` 区分：UserMessage 表示真实用户输入，渲染时
    显示 ``myc > `` 前缀；ReminderEvent 表示系统级注入，渲染时按
    提醒样式（如黄色高亮）展示。
    """
    model: str
    content: str
    id: str = ""
    parent_id: Optional[str] = None
    entry_type: str = "reminder"
    time: str = ""

    def to_user_msg(self) -> ChatCompletionUserMessageParam:
        """转为 ``ChatCompletionUserMessageParam``（喂给模型）。

        提示内容用 ``<reminder>`` 标签包裹，便于模型识别为系统注入。
        """
        return ChatCompletionUserMessageParam(
            role='user', content=f"<reminder>{self.content}</reminder>"
        )


AgentMessage = (
    SessionRecord
    | UserMessage
    | AssistantMessage
    | ToolCallEvent
    | ToolResultEvent
    | InterruptEvent
    | ExceptionEvent
    | ReminderEvent
)


def assert_never(arg: NoReturn) -> NoReturn:
    raise AssertionError(f"未处理的消息类型: {type(arg).__name__}")


def _msg_to_dict(msg: AgentMessage) -> Dict[str, Any]:
    """将 AgentMessage 转为 JSONL 字典"""
    d: Dict[str, Any] = {
        "time": msg.time,
        "type": msg.entry_type,
        "id": msg.id,
        "parent_id": msg.parent_id,
        "model": msg.model,
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
        case ToolResultEvent(message=message, tool_name=tool_name):
            d["message"] = message
            if tool_name:
                d["tool_name"] = tool_name
        case InterruptEvent():
            pass
        case ExceptionEvent(exception=exc_data):
            d["exception"] = exc_data
        case ReminderEvent(content=content):
            d["content"] = content
        case _ as unreachable:
            assert_never(unreachable)
    return d


def _dict_to_agent_message(data: Dict[str, Any]) -> AgentMessage | None:
    """将 JSONL 字典转为 AgentMessage"""
    entry_type = data.get("type")
    if entry_type not in ("message", "tool_call", "interrupt", "session", "exception", "reminder"):
        raise ValueError(f"未知的条目类型: {entry_type}")
    base_kwargs = {
        "id": data["id"],
        "parent_id": data.get("parent_id"),
        "model": data["model"],
        "time": data["time"],
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
        elif role == "tool":
            return ToolResultEvent(
                message=cast(ChatCompletionToolMessageParam, msg),
                tool_name=data.get("tool_name", ""),
                **base_kwargs,
            )
    elif entry_type == "tool_call":
        tc_data = data.get("tool_call")
        if not isinstance(tc_data, dict) or tc_data.get("type") != "function":
            raise ValueError(f"无效的 tool_call 数据: {tc_data}")
        return ToolCallEvent(
            tool_call=cast(ChatCompletionMessageFunctionToolCallParam, cast(Any, tc_data)),
            **base_kwargs,
        )
    elif entry_type == "interrupt":
        return InterruptEvent(**base_kwargs)
    elif entry_type == "exception":
        exc_data = data.get("exception", {})
        return ExceptionEvent(exception=cast(ExceptionData, exc_data), **base_kwargs)
    elif entry_type == "reminder":
        content = data.get("content", "")
        return ReminderEvent(content=content, **base_kwargs)
    # unreachable
    raise ValueError(f"未知的条目类型: {entry_type}")


class SessionHistory:
    """会话历史记录管理器"""

    def __init__(self, cwd: str, model: str):
        self.cwd = cwd
        self.model = model
        sanitized_cwd = sanitize_path(cwd)
        self.directory = SESSIONS_DIR / sanitized_cwd
        self.directory.mkdir(parents=True, exist_ok=True)

        # 生成完整UUID，用作文件名和session结构体的id
        self.session_uuid = str(uuid.uuid4())

        self.file_name = f"{self.session_uuid}.jsonl"
        self.file_path = self.directory / self.file_name

        self.entries: List[AgentMessage] = []

        # 写入初始 session 记录
        session_data = SessionData(id=self.session_uuid, cwd=cwd)
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
        """注入 id、parent_id、time 元数据"""
        msg.id = self._next_id()
        msg.parent_id = self.entries[-1].id if self.entries else None
        msg.time = get_iso_timestamp()

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
                    # 所有类型都加入 entries
                    agent_msg = _dict_to_agent_message(data)
                    if agent_msg is not None:
                        instance.entries.append(agent_msg)

        if not instance.entries:
            raise ValueError(f"无法加载空会话文件: {file_path}")
        first = instance.entries[0]
        if not isinstance(first, SessionRecord):
            raise ValueError(f"无效的会话文件：第一条记录不是session记录: {file_path}")

        return instance

    def get_messages(self) -> List[ChatCompletionMessageParam]:
        """获取所有消息（不含 session/interrupt/tool_call/exception 记录）。

        ``ReminderEvent``（系统级提醒）会通过 ``to_user_msg()`` 转成
        ``ChatCompletionUserMessageParam`` 加入返回列表，确保会话恢复后
        reminder 仍能进入模型上下文。
        """
        result: List[ChatCompletionMessageParam] = []
        for e in self.entries:
            if isinstance(e, (UserMessage, AssistantMessage, ToolResultEvent)):
                result.append(e.message)
            elif isinstance(e, ReminderEvent):
                result.append(e.to_user_msg())
        return result
