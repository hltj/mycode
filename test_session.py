"""会话历史模块测试"""
import json
from datetime import datetime
from unittest.mock import patch

import pytest

from session import (
    sanitize_path,
    get_iso_timestamp,
    SessionHistory,
    SessionEntry,
    SessionSession,
    SessionMessage,
    SessionToolCall,
)
from openai.types.chat import (
    ChatCompletionUserMessageParam,
    ChatCompletionAssistantMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionMessageFunctionToolCall,
)


@pytest.fixture
def temp_home(tmp_path):
    """创建临时home目录"""
    return tmp_path / ".mycode"


class TestSanitizePath:
    def test_normal_path(self):
        # 无特殊字符的路径保持不变
        assert sanitize_path("home-user-project") == "home-user-project"

    def test_slash_replaced(self):
        # / 被替换为 -，开头保留 -
        assert sanitize_path("/home/user/project") == "-home-user-project"

    def test_tilde_preserved(self):
        # ~ 不是路径元字符，保持不变
        assert sanitize_path("~/project") == "~-project"

    def test_backslash_replaced(self):
        # \ 是 Windows 路径分隔符，被替换为 -
        # C:\Users\project -> : 和 \ 都被替换
        assert sanitize_path("C:\\Users\\project") == "C--Users-project"

    def test_colon_replaced(self):
        # : 是 Windows 驱动器盘符分隔符
        assert sanitize_path("C:/path") == "C--path"

    def test_multiple_special_chars_replaced(self):
        result = sanitize_path("/home\\user~C:/test")
        assert "/" not in result
        assert "\\" not in result
        assert "~" in result  # ~ 不被替换
        assert ":" not in result

    def test_empty_path(self):
        # 空路径抛异常
        with pytest.raises(ValueError, match="不能为空"):
            sanitize_path("")

    def test_root_path(self):
        # 根路径 "/" 替换后为 "-"，非空
        assert sanitize_path("/") == "-"

    def test_spaces_preserved(self):
        # 空格不是路径元字符，保持不变
        assert sanitize_path("my project") == "my project"

    def test_windows_illegal_chars_replaced(self):
        # Windows 非法文件名字符被替换
        result = sanitize_path("test?file*name\"with<special>chars|here")
        assert "?" not in result
        assert "*" not in result
        assert '"' not in result
        assert "<" not in result
        assert ">" not in result
        assert "|" not in result

    def test_leading_dashes_preserved(self):
        # 开头的 - 保留
        assert sanitize_path("-secret") == "-secret"


class TestGetIsoTimestamp:
    def test_format(self):
        ts = get_iso_timestamp()
        # 应该包含T和时区信息
        assert "T" in ts

    def test_has_timezone(self):
        ts = get_iso_timestamp()
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None


class TestSessionEntry:
    def test_from_dict_session(self):
        data = {
            "time": "2026-04-08T16:06:50.231000+08:00",
            "type": "session",
            "id": "ed66ff3e",
            "parent_id": None,
            "model": "test-model",
            "session": {"id": "test-session-id", "cwd": "/home"}
        }
        entry = SessionEntry.from_dict(data)
        assert isinstance(entry, SessionSession)

    def test_from_dict_message(self):
        data = {
            "time": "2026-04-08T16:06:54.492000+08:00",
            "type": "message",
            "id": "a16dfd27",
            "parent_id": "ed66ff3e",
            "model": "test-model",
            "message": {"role": "user", "content": "test"}
        }
        entry = SessionEntry.from_dict(data)
        assert isinstance(entry, SessionMessage)

    def test_from_dict_invalid_type(self):
        data = {"type": "unknown"}
        with pytest.raises(ValueError, match="未知的条目类型"):
            SessionEntry.from_dict(data)


class TestSessionHistory:
    @pytest.fixture
    def session_history(self, temp_home):
        """创建会话历史实例"""
        sessions_dir = temp_home / "sessions"
        with patch('session.SESSIONS_DIR', sessions_dir):
            return SessionHistory("/test/project", model="test-model")

    def test_creates_directory(self, temp_home, session_history):
        """测试目录是否被正确创建"""
        assert session_history.directory.exists()
        assert session_history.directory.is_dir()

    def test_creates_jsonl_file(self, session_history):
        """测试JSONL文件是否被创建"""
        assert session_history.file_path.exists()
        assert session_history.file_path.suffix == ".jsonl"

    def test_initial_session_record(self, session_history):
        """测试初始session记录"""
        with open(session_history.file_path, 'r') as f:
            first_line = json.loads(f.readline())
            assert first_line["type"] == "session"
            assert first_line["parent_id"] is None
            assert "session" in first_line

    def test_append_message(self, session_history):
        """测试追加消息"""
        msg = ChatCompletionUserMessageParam(role="user", content="Hello")
        session_history.append_message(msg, model="test-model")

        with open(session_history.file_path, 'r') as f:
            lines = f.readlines()
            assert len(lines) == 2
            message_data = json.loads(lines[1])
            assert message_data["type"] == "message"
            assert message_data["message"]["role"] == "user"

    def test_parent_id_chain(self, session_history):
        """测试父节点ID链"""
        session_history.append_message(ChatCompletionUserMessageParam(role="user", content="msg1"), model="test-model")
        session_history.append_message(ChatCompletionAssistantMessageParam(role="assistant", content="msg2"), model="test-model")

        with open(session_history.file_path, 'r') as f:
            entries = [json.loads(line) for line in f.readlines()]
            # session的parent_id为None
            assert entries[0]["parent_id"] is None
            # 第一条message的parent是session
            assert entries[1]["parent_id"] == entries[0]["id"]
            # 第二条message的parent是第一条message
            assert entries[2]["parent_id"] == entries[1]["id"]

    def test_load_from_file(self, session_history):
        """测试从文件加载会话历史"""
        session_history.append_message(ChatCompletionUserMessageParam(role="user", content="test"), model="test-model")

        loaded = SessionHistory.load(session_history.file_path)
        assert len(loaded.entries) == 2
        assert isinstance(loaded.entries[0], SessionSession)
        assert isinstance(loaded.entries[1], SessionMessage)
        # 验证 session_uuid 从 session 记录中恢复，而非文件名
        assert loaded.session_uuid == session_history.session_uuid

    def test_load_restores_all_attributes(self, session_history):
        """测试load后所有属性都被正确恢复"""
        session_history.append_message(ChatCompletionUserMessageParam(role="user", content="test"), model="test-model")

        loaded = SessionHistory.load(session_history.file_path)

        # 验证 cwd 被恢复
        assert loaded.cwd == "/test/project"

        # 验证 directory 被恢复
        assert loaded.directory == session_history.directory

        # 验证 file_name 被恢复
        assert loaded.file_name == session_history.file_name

        # 验证 _record_id 被恢复
        assert loaded._record_id is not None

        # 验证 session_uuid 从 session 记录中恢复，而非文件名
        assert loaded.session_uuid == session_history.session_uuid

    def test_get_messages(self, session_history):
        """测试获取所有消息"""
        session_history.append_message(ChatCompletionUserMessageParam(role="user", content="msg1"), model="test-model")
        session_history.append_message(ChatCompletionAssistantMessageParam(role="assistant", content="msg2"), model="test-model")

        messages = session_history.get_messages()
        assert len(messages) == 2
        assert messages[0]["content"] == "msg1"
        assert messages[1]["content"] == "msg2"

    def test_sanitize_preserves_leading_dashes(self, session_history):
        """测试 sanitize_path 保留开头的减号"""
        # /test/project -> -test-project (开头保留-)
        assert session_history.directory.name == "-test-project"

    def test_load_empty_file_raises_error(self, temp_home):
        """测试加载空文件抛异常"""
        sessions_dir = temp_home / "sessions"
        with patch('session.SESSIONS_DIR', sessions_dir):
            SessionHistory("/test", model="test-model")

        # 创建空文件
        empty_file = sessions_dir / "test" / "empty.jsonl"
        if not empty_file.parent.exists():
            empty_file.parent.mkdir(parents=True)
        empty_file.touch()

        with pytest.raises(ValueError, match="无法加载空会话文件"):
            SessionHistory.load(empty_file)

    def test_load_corrupted_json_raises_error(self, temp_home):
        """测试加载损坏的JSON文件抛异常"""
        sessions_dir = temp_home / "sessions"
        with patch('session.SESSIONS_DIR', sessions_dir):
            SessionHistory("/test", model="test-model")

        # 创建包含损坏JSON的文件
        corrupted_file = sessions_dir / "test" / "corrupted.jsonl"
        if not corrupted_file.parent.exists():
            corrupted_file.parent.mkdir(parents=True)
        corrupted_file.write_text("not valid json\n")

        with pytest.raises(json.JSONDecodeError):
            SessionHistory.load(corrupted_file)

    def test_load_missing_type_raises_error(self, temp_home):
        """测试加载缺少type字段的条目抛异常"""
        sessions_dir = temp_home / "sessions"
        with patch('session.SESSIONS_DIR', sessions_dir):
            SessionHistory("/test", model="test-model")

        # 创建包含无type字段条目的文件
        bad_file = sessions_dir / "test" / "badtype.jsonl"
        if not bad_file.parent.exists():
            bad_file.parent.mkdir(parents=True)
        bad_file.write_text('{"time": "2026-01-01T00:00:00+08:00", "id": "abc"}\n')

        with pytest.raises(ValueError, match="未知的条目类型"):
            SessionHistory.load(bad_file)

    def test_load_first_entry_not_session_raises_error(self, temp_home):
        """测试加载第一条不是session记录的条目抛异常"""
        sessions_dir = temp_home / "sessions"
        with patch('session.SESSIONS_DIR', sessions_dir):
            SessionHistory("/test", model="test-model")

        bad_file = sessions_dir / "test" / "badorder.jsonl"
        if not bad_file.parent.exists():
            bad_file.parent.mkdir(parents=True)
        msg_line = (
            '{"time": "2026-01-01T00:00:00+08:00", "type": "message", '
            '"id": "abc", "parent_id": null, '
            '"model": "test-model", '
            '"message": {"role": "user", "content": "hi"}}\n'
        )
        bad_file.write_text(msg_line)

        with pytest.raises(ValueError, match="第一条记录不是session记录"):
            SessionHistory.load(bad_file)


class TestSessionHistoryIntegration:
    def test_full_workflow(self, temp_home):
        """完整工作流测试"""
        sessions_dir = temp_home / "sessions"
        with patch('session.SESSIONS_DIR', sessions_dir):
            # 创建会话
            history = SessionHistory("/my project/test", model="test-model")

            # 斜杠在目录名中被替换，开头保留 -
            assert "/" not in history.directory.name
            assert history.directory.name.startswith("-")

            # 添加消息
            history.append_message(ChatCompletionUserMessageParam(role="user", content="Hello"), model="test-model")
            history.append_message(ChatCompletionAssistantMessageParam(role="assistant", content="Hi!"), model="test-model")

            # 加载并验证
            loaded = SessionHistory.load(history.file_path)
            messages = loaded.get_messages()

            assert len(messages) == 2
            assert messages[0]["role"] == "user"
            assert messages[1]["role"] == "assistant"


class TestSessionHistoryFileStructure:
    """测试JSONL文件结构符合规范"""

    @pytest.fixture
    def session_history(self, temp_home):
        sessions_dir = temp_home / "sessions"
        with patch('session.SESSIONS_DIR', sessions_dir):
            return SessionHistory("/test/project", model="test-model")

    def test_session_entry_structure(self, session_history):
        """session条目应包含所有必需字段"""
        with open(session_history.file_path, 'r') as f:
            data = json.loads(f.readline())

        assert "time" in data
        assert "type" in data
        assert "id" in data
        assert "parent_id" in data
        assert "session" in data
        # parent_id应为null
        assert data["parent_id"] is None
        # session应有id和cwd
        assert "id" in data["session"]
        assert "cwd" in data["session"]

    def test_message_entry_structure(self, session_history):
        """message条目应包含所有必需字段"""
        session_history.append_message(ChatCompletionUserMessageParam(role="user", content="test"), model="test-model")

        with open(session_history.file_path, 'r') as f:
            lines = f.readlines()
            message_data = json.loads(lines[1])

        assert "time" in message_data
        assert message_data["type"] == "message"
        assert "id" in message_data
        assert "parent_id" in message_data
        assert "message" in message_data

    def test_time_is_iso8601_with_timezone(self, session_history):
        """时间应为ISO8601格式带时区"""
        with open(session_history.file_path, 'r') as f:
            data = json.loads(f.readline())

        time_str = data["time"]
        dt = datetime.fromisoformat(time_str)
        assert dt.tzinfo is not None

    def test_id_length(self, session_history):
        """session记录id应为8个字符（短UUID）"""
        with open(session_history.file_path, 'r') as f:
            data = json.loads(f.readline())

        assert len(data["id"]) == 8

    def test_file_name_matches_session_id(self, session_history):
        """文件名使用完整UUID，记录id用短ID(前8位)"""
        with open(session_history.file_path, 'r') as f:
            data = json.loads(f.readline())

        # 文件名是完整UUID
        assert len(session_history.file_path.stem) == 36
        # session.id 也是完整UUID，与文件名一致
        assert session_history.file_path.stem == data["session"]["id"]
        # 记录id是短ID(前8位)
        assert len(data["id"]) == 8
        assert data["id"] == session_history.file_path.stem[:8]


class TestIdCollision:
    """测试短ID冲突时改用完整UUID"""

    def test_short_id_collision_uses_full_uuid(self, temp_home):
        """短id冲突时改用完整uuid"""
        import uuid as uu
        sessions_dir = temp_home / "sessions"
        with patch('session.SESSIONS_DIR', sessions_dir):
            # 第一个uuid用于创建session，后面两个用于追加消息
            call_count = [0]

            def mock_uuid4():
                call_count[0] += 1
                if call_count[0] == 1:
                    # session创建用这个，不冲突
                    return uu.UUID('bbbbbbbb-cccc-dddd-eeee-ffffffffffff')
                elif call_count[0] == 2:
                    # 第一次消息，短id aaaaaaaa
                    return uu.UUID('aaaaaaaa-1111-2222-3333-444444444444')
                else:
                    # 第二次消息，短id还是 aaaaaaaa 导致冲突
                    return uu.UUID('aaaaaaaa-5555-6666-7777-888888888888')

            with patch('session.uuid.uuid4', side_effect=mock_uuid4):
                history = SessionHistory("/test", model="test-model")
                history.append_message(ChatCompletionUserMessageParam(role="user", content="msg1"), model="test-model")
                history.append_message(ChatCompletionAssistantMessageParam(role="assistant", content="msg2"), model="test-model")

            loaded = SessionHistory.load(history.file_path)
            entries = [e for e in loaded.entries if isinstance(e, SessionMessage)]

            # 第一条消息用短id aaaaaaaa
            assert entries[0].id == "aaaaaaaa"
            # 第二条消息因冲突使用完整uuid（长度36，带横杠）
            assert len(entries[1].id) == 36
            assert entries[1].id == "aaaaaaaa-5555-6666-7777-888888888888"


class TestToolCallsSerialization:
    """测试 tool_calls 的 JSON 序列化与反序列化"""

    def test_assistant_message_with_tool_calls_serializes(self, temp_home):
        """测试带 tool_calls 的 assistant message 能正确序列化为 JSON"""
        sessions_dir = temp_home / "sessions"
        with patch('session.SESSIONS_DIR', sessions_dir):
            history = SessionHistory("/test", model="gpt-4o")

            tc = ChatCompletionMessageFunctionToolCall(
                id="call_abc",
                type="function",
                function={"name": "bash", 'arguments': '{"command": "echo hi"}'}
            )
            msg = ChatCompletionAssistantMessageParam(
                role="assistant",
                content="",
                tool_calls=[tc]
            )
            history.append_message(msg, "gpt-4o")

        # 验证写入的文件能被正确解析为 JSON
        with open(history.file_path) as f:
            for line in f:
                data = json.loads(line.strip())
                if data["type"] == "message" and data["message"]["role"] == "assistant":
                    tc_data = data["message"]["tool_calls"][0]
                    assert isinstance(tc_data, dict)
                    assert tc_data["id"] == "call_abc"
                    assert tc_data["function"]["name"] == "bash"

    def test_assistant_message_with_tool_calls_roundtrip(self, temp_home):
        """测试带 tool_calls 的 assistant message 能正确加载并恢复"""
        sessions_dir = temp_home / "sessions"
        with patch('session.SESSIONS_DIR', sessions_dir):
            history = SessionHistory("/test", model="gpt-4o")

            tc = ChatCompletionMessageFunctionToolCall(
                id="call_xyz",
                type="function",
                function={"name": "ls", 'arguments': "{}"}
            )
            msg = ChatCompletionAssistantMessageParam(
                role="assistant",
                content="Let me check.",
                tool_calls=[tc]
            )
            history.append_message(msg, "gpt-4o")

        # 加载并验证
        loaded = SessionHistory.load(history.file_path)
        messages = loaded.get_messages()
        assert len(messages) == 1
        assistant_msg = messages[0]
        tc_loaded = assistant_msg["tool_calls"][0]
        assert tc_loaded["id"] == "call_xyz"
        assert tc_loaded["function"]["name"] == "ls"

    def test_tool_call_entry_serializes_and_loads(self, temp_home):
        """测试 tool_call 类型条目能正确序列化和加载"""
        sessions_dir = temp_home / "sessions"
        with patch('session.SESSIONS_DIR', sessions_dir):
            history = SessionHistory("/test", model="gpt-4o")

            tc = ChatCompletionMessageFunctionToolCall(
                id="call_tc_test",
                type="function",
                function={"name": "cat", 'arguments': '{"path": "test.txt"}'}
            )
            history.append_tool_call(tc, "gpt-4o")

        # 验证 JSON 文件中包含 tool_call 类型
        with open(history.file_path) as f:
            lines = [json.loads(l.strip()) for l in f if l.strip()]
            tc_lines = [l for l in lines if l["type"] == "tool_call"]
            assert len(tc_lines) == 1
            assert tc_lines[0]["model"] == "gpt-4o"
            assert tc_lines[0]["tool_call"]["id"] == "call_tc_test"
            assert tc_lines[0]["tool_call"]["function"]["name"] == "cat"

        # 加载并验证
        loaded = SessionHistory.load(history.file_path)
        tc_entries = [e for e in loaded.entries if isinstance(e, SessionToolCall)]
        assert len(tc_entries) == 1
        tc_entry = tc_entries[0]
        assert tc_entry.tool_call.id == "call_tc_test"
        assert tc_entry.tool_call.function.name == "cat"
        assert tc_entry.model == "gpt-4o"

    def test_full_tool_call_workflow(self, temp_home):
        """完整工作流：assistant -> tool_call -> tool_result"""
        sessions_dir = temp_home / "sessions"
        with patch('session.SESSIONS_DIR', sessions_dir):
            history = SessionHistory("/test", model="gpt-4o")

            # assistant 回复带 tool_calls
            tc_obj = ChatCompletionMessageFunctionToolCall(
                id="call_work",
                type="function",
                function={"name": "bash", 'arguments': '{"command": "pwd"}'}
            )
            history.append_message(ChatCompletionAssistantMessageParam(
                role="assistant", content="", tool_calls=[tc_obj]
            ), "gpt-4o")

            # tool_call 记录
            history.append_tool_call(tc_obj, "gpt-4o")

            # tool result
            history.append_message(ChatCompletionToolMessageParam(
                role="tool", tool_call_id="call_work", content="/home"
            ), "gpt-4o")

        # 加载并验证完整流程
        loaded = SessionHistory.load(history.file_path)
        messages = loaded.get_messages()
        assert len(messages) == 2  # assistant + tool
        assert messages[0]["role"] == "assistant"
        assert messages[1]["role"] == "tool"
        assert messages[1]["content"] == "/home"

        # 验证 entries 顺序
        entry_types = [e.type for e in loaded.entries if e.type != "session"]
        assert entry_types == ["message", "tool_call", "message"]
