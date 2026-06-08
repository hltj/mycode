"""
会话历史记录模块
"""

from __future__ import annotations

import os
import uuid
import json
from pathlib import Path
from datetime import datetime
from typing import TypedDict, Optional, List, Dict, Any

from openai.types.chat import ChatCompletionMessageParam
from openai.types.chat import ChatCompletionMessageFunctionToolCall

# 应用目录
APP_HOME_DIR = Path(os.getenv('MYCODE_HOME_DIR', os.path.expanduser('~/.mycode')))
SESSIONS_DIR = APP_HOME_DIR / "sessions"


class SessionData(TypedDict):
    """会话元数据"""
    id: str
    cwd: str


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


class SessionEntry:
    """会话条目基类"""
    
    def __init__(self, time: str, entry_type: str, id_: str, parent_id: Optional[str], model: str):
        self.time = time
        self.type = entry_type
        self.id = id_
        self.parent_id = parent_id
        self.model = model
    
    def to_dict_base(self) -> Dict[str, Any]:
        return {
            "time": self.time,
            "type": self.type,
            "id": self.id,
            "parent_id": self.parent_id,
            "model": self.model,
        }

    def to_dict(self) -> Dict[str, Any]:
        raise NotImplementedError
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SessionEntry:
        if data.get("type") == "session":
            return SessionSession.from_dict(data)
        elif data.get("type") == "message":
            return SessionMessage.from_dict(data)
        elif data.get("type") == "tool_call":
            return SessionToolCall.from_dict(data)
        raise ValueError(f"未知的条目类型: {data.get('type')}")


class SessionSession(SessionEntry):
    """session 类型的条目"""
    
    def __init__(self, time: str, id_: str, session: SessionData, model: str):
        super().__init__(time, "session", id_, None, model)
        self.session = session
    
    def to_dict(self) -> Dict[str, Any]:
        data = self.to_dict_base()
        data["session"] = self.session
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SessionSession:
        return cls(
            time=data["time"],
            id_=data["id"],
            session=data["session"],
            model=data["model"],
        )


class SessionMessage(SessionEntry):
    """message 类型的条目"""
    
    def __init__(self, time: str, id_: str, parent_id: str, message: ChatCompletionMessageParam, model: str):
        super().__init__(time, "message", id_, parent_id, model)
        self.message = message
    
    @staticmethod
    def _serialize_message(message: ChatCompletionMessageParam) -> ChatCompletionMessageParam:
        """递归序列化消息中的 Pydantic 对象（如 tool_calls）为 dict"""
        if "tool_calls" in message and message["tool_calls"]:
            serialized_calls = []
            for tc in message["tool_calls"]:
                if hasattr(tc, "model_dump"):
                    serialized_calls.append(tc.model_dump())
                else:
                    serialized_calls.append(tc)
            return {**message, "tool_calls": serialized_calls}
        return message

    def to_dict(self) -> Dict[str, Any]:
        data = self.to_dict_base()
        data["message"] = self._serialize_message(self.message)
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SessionMessage:
        return cls(
            time=data["time"],
            id_=data["id"],
            parent_id=data["parent_id"],
            message=data["message"],
            model=data["model"],
        )


class SessionToolCall(SessionEntry):
    """tool_call 类型的条目，记录工具调用的原始信息"""
    
    def __init__(self, time: str, id_: str, parent_id: str, tool_call: ChatCompletionMessageFunctionToolCall, model: str):
        super().__init__(time, "tool_call", id_, parent_id, model)
        self.tool_call = tool_call
    
    def to_dict(self) -> Dict[str, Any]:
        data = self.to_dict_base()
        data["tool_call"] = self.tool_call.model_dump()
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SessionToolCall:
        tc_data = data.get("tool_call")
        if not isinstance(tc_data, dict) or tc_data.get("type") != "function":
            raise ValueError(f"无效的 tool_call 数据: {tc_data}")
        return cls(
            time=data["time"],
            id_=data["id"],
            parent_id=data["parent_id"],
            tool_call=ChatCompletionMessageFunctionToolCall.model_validate(tc_data),
            model=data["model"],
        )


class SessionHistory:
    """会话历史记录管理器"""
    
    def __init__(self, cwd: str, model: str):
        self.cwd = cwd
        self.model = model
        # 将cwd中的特殊符号替换为减号
        sanitized_cwd = sanitize_path(cwd)
        self.directory = SESSIONS_DIR / sanitized_cwd
        self.directory.mkdir(parents=True, exist_ok=True)
        
        # 生成完整UUID，用作文件名和session结构体的id
        self.session_uuid = str(uuid.uuid4())
        # 记录的id用短ID（前8位），如果冲突则改用完整UUID
        short_id = self.session_uuid[:8]
        self._record_id = short_id  # 新文件不会有冲突，直接用短id
        
        self.file_name = f"{self.session_uuid}.jsonl"
        self.file_path = self.directory / self.file_name
        
        self.entries: List[SessionEntry] = []
        self._last_id: Optional[str] = None
        
        # 创建初始session记录，session.id 使用完整UUID
        session_data = SessionData(
            id=self.session_uuid,
            cwd=cwd
        )
        session_entry = SessionSession(
            time=get_iso_timestamp(),
            id_=self._record_id,
            session=session_data,
            model=self.model,
        )
        self.entries.append(session_entry)
        self._last_id = self._record_id
        
        # 写入session记录
        self._append_to_file(session_entry.to_dict())
    
    def _append_to_file(self, data: Dict[str, Any]):
        """追加一行JSON到文件"""
        with open(self.file_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(data, ensure_ascii=False) + '\n')
    
    def append_message(self, message: ChatCompletionMessageParam, model: str):
        """追加消息"""
        full_uuid = str(uuid.uuid4())
        short_id = full_uuid[:8]
        # 收集已有 id 用于冲突检测
        existing_ids = {e.id for e in self.entries}
        record_id = short_id if short_id not in existing_ids else full_uuid
        assert self._last_id is not None
        entry = SessionMessage(
            time=get_iso_timestamp(),
            id_=record_id,
            parent_id=self._last_id,
            message=message,
            model=model,
        )
        self.entries.append(entry)
        self._last_id = entry.id
        # 追加写入到文件
        self._append_to_file(entry.to_dict())
    
    def append_tool_call(self, tool_call: ChatCompletionMessageFunctionToolCall, model: str):
        """追加工具调用记录"""
        full_uuid = str(uuid.uuid4())
        short_id = full_uuid[:8]
        existing_ids = {e.id for e in self.entries}
        record_id = short_id if short_id not in existing_ids else full_uuid
        assert self._last_id is not None
        entry = SessionToolCall(
            time=get_iso_timestamp(),
            id_=record_id,
            parent_id=self._last_id,
            tool_call=tool_call,
            model=model,
        )
        self.entries.append(entry)
        self._last_id = entry.id
        self._append_to_file(entry.to_dict())
    
    @classmethod
    def load(cls, file_path: Path) -> SessionHistory:
        """从JSONL文件加载会话历史"""
        instance = cls.__new__(cls)
        instance.file_path = file_path
        instance.session_uuid = None
        instance.file_name = file_path.name
        instance.entries = []
        instance._last_id = None
        
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    entry = SessionEntry.from_dict(data)
                    instance.entries.append(entry)
                    instance._last_id = entry.id
        
        if not instance.entries:
            raise ValueError(f"无法加载空会话文件: {file_path}")
        
        # 恢复 cwd、directory、_record_id、model
        first_entry = instance.entries[0]
        if isinstance(first_entry, SessionSession):
            instance.session_uuid = first_entry.session.get("id", "")
            instance.cwd = first_entry.session.get("cwd", "")
            instance.directory = file_path.parent
            instance._record_id = first_entry.id
            instance.model = first_entry.model
        else:
            raise ValueError("无效的会话文件：第一条记录不是session记录")
        
        return instance
    
    def get_messages(self) -> List[ChatCompletionMessageParam]:
        """获取所有消息（不含session记录）"""
        return [entry.message for entry in self.entries if isinstance(entry, SessionMessage)]
