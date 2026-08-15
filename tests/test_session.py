"""会话历史模块测试"""
import json
from datetime import datetime
from unittest.mock import patch

import pytest

from mycode.session import (
    sanitize_path,
    get_iso_timestamp,
    SessionHistory,
    SessionRecord,
    UserMessage,
    AssistantMessage,
    ToolCallEvent,
    ToolResultEvent,
    InterruptEvent,
    _dict_to_agent_message,
    _msg_to_dict,
    get_session_file,
    find_latest_session_file,
)

from openai.types.chat import (
    ChatCompletionUserMessageParam,
    ChatCompletionAssistantMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionMessageFunctionToolCallParam,
)
from openai.types.chat.chat_completion_message_function_tool_call_param import Function


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
        result = sanitize_path('test?file*name"with<special>chars|here')
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
        result = _dict_to_agent_message(data)
        assert isinstance(result, SessionRecord)
        assert result.session["id"] == "test-session-id"

    def test_from_dict_message(self):
        data = {
            "time": "2026-04-08T16:06:54.492000+08:00",
            "type": "message",
            "id": "a16dfd27",
            "parent_id": "ed66ff3e",
            "model": "test-model",
            "message": {"role": "user", "content": "test"}
        }
        entry = _dict_to_agent_message(data)
        assert isinstance(entry, UserMessage)

    def test_from_dict_interrupt(self):
        data = {
            "time": "2026-04-08T16:06:55.000000+08:00",
            "type": "interrupt",
            "id": "b27efd38",
            "parent_id": "ed66ff3e",
            "model": "test-model",
        }
        entry = _dict_to_agent_message(data)
        assert isinstance(entry, InterruptEvent)
        assert entry.abort is False

    def test_from_dict_interrupt_abort(self):
        """abort 标记的 InterruptEvent 往返。"""
        data = {
            "time": "2026-04-08T16:06:55.000000+08:00",
            "type": "interrupt",
            "id": "b27efd38",
            "parent_id": "ed66ff3e",
            "model": "test-model",
            "abort": True,
        }
        entry = _dict_to_agent_message(data)
        assert isinstance(entry, InterruptEvent)
        assert entry.abort is True
        # 往返：msg_to_dict 再读回
        d = _msg_to_dict(entry)
        assert d.get("abort") is True
        loaded = _dict_to_agent_message(d)
        assert isinstance(loaded, InterruptEvent)
        assert loaded.abort is True

    def test_from_dict_invalid_type(self):
        data = {"type": "unknown"}
        with pytest.raises(ValueError, match="未知的条目类型"):
            _dict_to_agent_message(data)

    def test_tool_result_event_with_tool_name(self):
        """ToolResultEvent 持久化 tool_name 字段。"""
        from mycode.session import ToolResultEvent, _msg_to_dict, _dict_to_agent_message
        from mycode.tools_registry import ToolsRegistry
        ev = ToolResultEvent(
            model="m",
            message={"role": "tool", "tool_call_id": "c1", "content": "ok"},
            tool_name="todo_write",
            id="t1",
        )
        d = _msg_to_dict(ev)
        assert d["tool_name"] == "todo_write"
        # 反向：tool_name 应被恢复
        roundtrip = _dict_to_agent_message(d)
        assert isinstance(roundtrip, ToolResultEvent)
        assert roundtrip.tool_name == "todo_write"

    def test_tool_result_event_without_tool_name_backward_compat(self):
        """旧 session 数据无 tool_name 字段也能正常加载（向后兼容）。"""
        from mycode.session import _dict_to_agent_message, ToolResultEvent
        data = {
            "time": "2026-04-08T16:06:54+08:00",
            "type": "message",
            "id": "x",
            "parent_id": None,
            "model": "m",
            "message": {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            # 无 tool_name
        }
        entry = _dict_to_agent_message(data)
        assert isinstance(entry, ToolResultEvent)
        assert entry.tool_name == ""  # 默认空字符串

    def test_reminder_roundtrips_new_fields(self):
        """ReminderEvent 的 display_content/additional_content 可经 JSONL 往返。"""
        from mycode.session import (
            ReminderEvent, _msg_to_dict, _dict_to_agent_message,
        )
        ev = ReminderEvent(
            model="m",
            content="用户将命令修改为：",
            display_content="命令修改为：",
            additional_content="```bash\nls -la\n```",
            id="r1",
        )
        d = _msg_to_dict(ev)
        assert d["type"] == "reminder"
        assert d["display_content"] == "命令修改为："
        assert d["additional_content"] == "```bash\nls -la\n```"
        loaded = _dict_to_agent_message(d)
        assert isinstance(loaded, ReminderEvent)
        assert loaded.content == ev.content
        assert loaded.display_content == ev.display_content
        assert loaded.additional_content == ev.additional_content

    def test_reminder_old_jsonl_no_new_fields(self):
        """旧 session 文件的 ReminderEvent 无新字段也能加载（向后兼容）。"""
        from mycode.session import _dict_to_agent_message, ReminderEvent
        data = {
            "time": "2026-04-08T16:06:54+08:00",
            "type": "reminder",
            "id": "r",
            "parent_id": None,
            "model": "m",
            "content": "有未完成的 todo",
        }
        entry = _dict_to_agent_message(data)
        assert isinstance(entry, ReminderEvent)
        assert entry.content == "有未完成的 todo"
        assert entry.display_content == ""
        assert entry.additional_content == ""

    def test_reminder_empty_fields_not_written(self):
        """新字段为空时不写入 JSONL（不含多余键）。"""
        from mycode.session import ReminderEvent, _msg_to_dict
        ev = ReminderEvent(model="m", content="hi")
        d = _msg_to_dict(ev)
        assert "display_content" not in d
        assert "additional_content" not in d


class TestSessionHistory:
    @pytest.fixture
    def session_history(self, temp_home):
        """创建会话历史实例"""
        sessions_dir = temp_home / "sessions"
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
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
        user_msg = UserMessage(message=msg, model="test-model")
        session_history.inject_meta(user_msg)
        session_history.append(user_msg)

        with open(session_history.file_path, 'r') as f:
            lines = f.readlines()
            assert len(lines) == 2
            message_data = json.loads(lines[1])
            assert message_data["type"] == "message"
            assert message_data["message"]["role"] == "user"

    def test_parent_id_chain(self, session_history):
        """测试父节点ID链"""
        user_msg1 = UserMessage(message=ChatCompletionUserMessageParam(role="user", content="msg1"), model="test-model")
        asst_msg = AssistantMessage(message=ChatCompletionAssistantMessageParam(role="assistant", content="msg2"), model="test-model")
        session_history.inject_meta(user_msg1)
        session_history.append(user_msg1)
        session_history.inject_meta(asst_msg)
        session_history.append(asst_msg)

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
        user_msg = UserMessage(message=ChatCompletionUserMessageParam(role="user", content="test"), model="test-model")
        session_history.inject_meta(user_msg)
        session_history.append(user_msg)

        loaded = SessionHistory.load(session_history.file_path)
        assert len(loaded.entries) == 2  # SessionRecord + UserMessage
        assert isinstance(loaded.entries[0], SessionRecord)
        assert isinstance(loaded.entries[1], UserMessage)
        # 验证 session_uuid 从 session 记录中恢复，而非文件名
        assert loaded.session_uuid == session_history.session_uuid

    def test_load_restores_all_attributes(self, session_history):
        """测试load后所有属性都被正确恢复"""
        user_msg = UserMessage(message=ChatCompletionUserMessageParam(role="user", content="test"), model="test-model")
        session_history.inject_meta(user_msg)
        session_history.append(user_msg)

        loaded = SessionHistory.load(session_history.file_path)

        # 验证 cwd 被恢复
        assert loaded.cwd == "/test/project"

        # 验证 directory 被恢复
        assert loaded.directory == session_history.directory

        # 验证 file_name 被恢复
        assert loaded.file_name == session_history.file_name

        # 验证第一条记录 id 被恢复
        assert loaded.entries[0].id is not None

        # 验证 session_uuid 从 session 记录中恢复，而非文件名
        assert loaded.session_uuid == session_history.session_uuid

    def test_get_messages(self, session_history):
        """测试获取所有消息"""
        user_msg = UserMessage(message=ChatCompletionUserMessageParam(role="user", content="msg1"), model="test-model")
        asst_msg = AssistantMessage(message=ChatCompletionAssistantMessageParam(role="assistant", content="msg2"), model="test-model")
        session_history.inject_meta(user_msg)
        session_history.append(user_msg)
        session_history.inject_meta(asst_msg)
        session_history.append(asst_msg)

        messages = session_history.get_messages()
        assert len(messages) == 2
        assert messages[0]["content"] == "msg1"
        assert messages[1]["content"] == "msg2"

    def test_append_interrupt(self, session_history):
        """测试追加中断记录"""
        interrupt = InterruptEvent(model="test-model")
        session_history.inject_meta(interrupt)
        session_history.append(interrupt)

        with open(session_history.file_path, 'r') as f:
            lines = f.readlines()
            assert len(lines) == 2
            interrupt_data = json.loads(lines[1])
            assert interrupt_data["type"] == "interrupt"

    def test_append_interrupt_parent_id(self, session_history):
        """测试中断记录的父节点ID链"""
        session_history.append_message(ChatCompletionUserMessageParam(role="user", content="msg1"), model="test-model")
        interrupt = InterruptEvent(model="test-model")
        session_history.inject_meta(interrupt)
        session_history.append(interrupt)

        with open(session_history.file_path, 'r') as f:
            entries = [json.loads(line) for line in f.readlines()]
            # interrupt 的 parent 应该是 msg1
            assert entries[2]["parent_id"] == entries[1]["id"]

    def test_load_interrupt(self, session_history):
        """测试中断记录的加载"""
        user_msg = UserMessage(message=ChatCompletionUserMessageParam(role="user", content="test"), model="test-model")
        session_history.inject_meta(user_msg)
        session_history.append(user_msg)
        interrupt = InterruptEvent(model="test-model")
        session_history.inject_meta(interrupt)
        session_history.append(interrupt)

        loaded = SessionHistory.load(session_history.file_path)
        assert len(loaded.entries) == 3
        assert isinstance(loaded.entries[2], SessionInterrupt)
        assert loaded.entries[2].model == "test-model"

    def test_append_interrupt(self, session_history):
        """测试追加中断记录"""
        interrupt = InterruptEvent(model="test-model")
        session_history.inject_meta(interrupt)
        session_history.append(interrupt)

        with open(session_history.file_path, 'r') as f:
            lines = f.readlines()
            assert len(lines) == 2
            interrupt_data = json.loads(lines[1])
            assert interrupt_data["type"] == "interrupt"

    def test_append_interrupt_parent_id(self, session_history):
        """测试中断记录的父节点ID链"""
        user_msg = UserMessage(message=ChatCompletionUserMessageParam(role="user", content="msg1"), model="test-model")
        interrupt = InterruptEvent(model="test-model")
        session_history.inject_meta(user_msg)
        session_history.append(user_msg)
        session_history.inject_meta(interrupt)
        session_history.append(interrupt)

        with open(session_history.file_path, 'r') as f:
            entries = [json.loads(line) for line in f.readlines()]
            # session -> msg1 -> interrupt
            assert entries[0]["type"] == "session"
            # interrupt 的 parent 应该是 msg1
            assert entries[1]["parent_id"] == entries[0]["id"]
            assert entries[2]["parent_id"] == entries[1]["id"]

    def test_load_interrupt(self, session_history):
        """测试中断记录的加载"""
        user_msg = UserMessage(message=ChatCompletionUserMessageParam(role="user", content="test"), model="test-model")
        interrupt = InterruptEvent(model="test-model")
        session_history.inject_meta(user_msg)
        session_history.append(user_msg)
        session_history.inject_meta(interrupt)
        session_history.append(interrupt)

        loaded = SessionHistory.load(session_history.file_path)
        assert len(loaded.entries) == 3  # SessionRecord + UserMessage + InterruptEvent
        assert isinstance(loaded.entries[2], InterruptEvent)
        assert loaded.entries[2].model == "test-model"

    def test_sanitize_preserves_leading_dashes(self, session_history):
        """测试 sanitize_path 保留开头的减号"""
        # /test/project -> -test-project (开头保留-)
        assert session_history.directory.name == "-test-project"

    def test_load_empty_file_raises_error(self, temp_home):
        """测试加载空文件抛异常"""
        sessions_dir = temp_home / "sessions"
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
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
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
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
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
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
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
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
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
            # 创建会话
            history = SessionHistory("/my project/test", model="test-model")

            # 斜杠在目录名中被替换，开头保留 -
            assert "/" not in history.directory.name
            assert history.directory.name.startswith("-")

            # 添加消息
            user_msg = UserMessage(message=ChatCompletionUserMessageParam(role="user", content="Hello"), model="test-model")
            asst_msg = AssistantMessage(message=ChatCompletionAssistantMessageParam(role="assistant", content="Hi!"), model="test-model")
            history.inject_meta(user_msg)
            history.append(user_msg)
            history.inject_meta(asst_msg)
            history.append(asst_msg)

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
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
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
        user_msg = UserMessage(message=ChatCompletionUserMessageParam(role="user", content="test"), model="test-model")
        session_history.inject_meta(user_msg)
        session_history.append(user_msg)

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

    def test_interrupt_entry_structure(self, session_history):
        """interrupt条目应包含所有必需字段"""
        interrupt = InterruptEvent(model="test-model")
        session_history.inject_meta(interrupt)
        session_history.append(interrupt)

        with open(session_history.file_path, 'r') as f:
            lines = f.readlines()
            interrupt_data = json.loads(lines[1])

        assert "time" in interrupt_data
        assert interrupt_data["type"] == "interrupt"
        assert "id" in interrupt_data
        assert "parent_id" in interrupt_data
        assert "model" in interrupt_data
        assert interrupt_data["model"] == "test-model"

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
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
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

            with patch('mycode.session.uuid.uuid4', side_effect=mock_uuid4):
                history = SessionHistory("/test", model="test-model")
                user_msg = UserMessage(message=ChatCompletionUserMessageParam(role="user", content="msg1"), model="test-model")
                asst_msg = AssistantMessage(message=ChatCompletionAssistantMessageParam(role="assistant", content="msg2"), model="test-model")
                history.inject_meta(user_msg)
                history.append(user_msg)
                history.inject_meta(asst_msg)
                history.append(asst_msg)

            loaded = SessionHistory.load(history.file_path)

            # entries[0] 是 SessionRecord，entries[1] 是第一条 UserMessage
            assert isinstance(loaded.entries[0], SessionRecord)
            # 第一条消息用短id aaaaaaaa
            assert isinstance(loaded.entries[1], UserMessage)
            assert loaded.entries[1].message["content"] == "msg1"
            assert loaded.entries[1].id == "aaaaaaaa"
            # 第二条消息因冲突使用完整uuid（长度36，带横杠）
            assert isinstance(loaded.entries[2], AssistantMessage)
            assert len(loaded.entries[2].id) == 36
            assert loaded.entries[2].id == "aaaaaaaa-5555-6666-7777-888888888888"


class TestToolCallsSerialization:
    """测试 tool_calls 的 JSON 序列化与反序列化"""

    def test_assistant_message_with_tool_calls_serializes(self, temp_home):
        """测试带 tool_calls 的 assistant message 能正确序列化为 JSON"""
        sessions_dir = temp_home / "sessions"
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
            history = SessionHistory("/test", model="gpt-4o")

            tc = ChatCompletionMessageFunctionToolCallParam(
                id="call_abc",
                type="function",
                function=Function(name="bash", arguments='{"command": "echo hi"}'),
            )
            msg = ChatCompletionAssistantMessageParam(
                role="assistant",
                content="",
                tool_calls=[tc]
            )
            asst_msg = AssistantMessage(message=msg, model="gpt-4o")
            history.inject_meta(asst_msg)
            history.append(asst_msg)

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
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
            history = SessionHistory("/test", model="gpt-4o")

            tc = ChatCompletionMessageFunctionToolCallParam(
                id="call_xyz",
                type="function",
                function=Function(name="ls", arguments="{}"),
            )
            msg = ChatCompletionAssistantMessageParam(
                role="assistant",
                content="Let me check.",
                tool_calls=[tc]
            )
            asst_msg = AssistantMessage(message=msg, model="gpt-4o")
            history.inject_meta(asst_msg)
            history.append(asst_msg)

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
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
            history = SessionHistory("/test", model="gpt-4o")

            tc = ChatCompletionMessageFunctionToolCallParam(
                id="call_tc_test",
                type="function",
                function=Function(name="cat", arguments='{"path": "test.txt"}'),
            )
            tool_call_evt = ToolCallEvent(tool_call=tc, model="gpt-4o")
            history.inject_meta(tool_call_evt)
            history.append(tool_call_evt)

        # 验证 JSON 文件中包含 tool_call 类型
        with open(history.file_path) as f:
            lines = [json.loads(l.strip()) for l in f if l.strip()]
            tc_lines = [l for l in lines if l["type"] == "tool_call"]
            assert len(tc_lines) == 1
            assert tc_lines[0]["model"] == "gpt-4o"
            assert tc_lines[0]["tool_call"]["id"] == "call_tc_test"
            assert tc_lines[0]["tool_call"]["function"]["name"] == "cat"

        # 加载并验证（tool_call 是 dict）
        loaded = SessionHistory.load(history.file_path)
        tc_entries = [e for e in loaded.entries if isinstance(e, ToolCallEvent)]
        assert len(tc_entries) == 1
        tc_entry = tc_entries[0]
        assert tc_entry.tool_call["id"] == "call_tc_test"
        assert tc_entry.tool_call["function"]["name"] == "cat"
        assert tc_entry.model == "gpt-4o"

    def test_full_tool_call_workflow(self, temp_home):
        """完整工作流：assistant -> tool_call -> tool_result"""
        sessions_dir = temp_home / "sessions"
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
            history = SessionHistory("/test", model="gpt-4o")

            # assistant 回复带 tool_calls（ChatCompletionMessageFunctionToolCallParam 形式）
            tc = ChatCompletionMessageFunctionToolCallParam(
                id="call_work",
                type="function",
                function=Function(name="bash", arguments='{"command": "pwd"}'),
            )
            asst_msg = AssistantMessage(message=ChatCompletionAssistantMessageParam(
                role="assistant", content="", tool_calls=[tc]
            ), model="gpt-4o")
            history.inject_meta(asst_msg)
            history.append(asst_msg)

            # tool_call 记录（ChatCompletionMessageFunctionToolCallParam 形式）
            tool_call_evt = ToolCallEvent(tool_call=tc, model="gpt-4o")
            history.inject_meta(tool_call_evt)
            history.append(tool_call_evt)

            # tool result
            tool_result = ToolResultEvent(message=ChatCompletionToolMessageParam(
                role="tool", tool_call_id="call_work", content="/home"
            ), model="gpt-4o")
            history.inject_meta(tool_result)
            history.append(tool_result)

        # 加载并验证完整流程
        loaded = SessionHistory.load(history.file_path)
        messages = loaded.get_messages()
        assert len(messages) == 2  # assistant + tool
        assert messages[0]["role"] == "assistant"
        assert messages[1]["role"] == "tool"
        assert messages[1]["content"] == "/home"

        # 验证 entries 顺序
        entry_types = [e.entry_type for e in loaded.entries if e.entry_type != "session"]
        assert entry_types == ["message", "tool_call", "message"]



class TestGetSessionFile:
    """测试 get_session_file"""

    def test_existing_file(self, temp_home):
        """存在时返回路径"""
        sessions_dir = temp_home / "sessions"
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
            history = SessionHistory("/test", model="test-model")

        loaded = SessionHistory.load(history.file_path)
        session_id = loaded.session_uuid

        # get_session_file 内部用 session.SESSIONS_DIR 和 session.os.getcwd()，需同时 patch
        with patch('mycode.session.SESSIONS_DIR', sessions_dir), patch('mycode.session.os.getcwd', return_value="/test"):
            result = get_session_file(session_id)
        assert result is not None
        assert result == history.file_path

    def test_non_existing_file(self, temp_home):
        """不存在时返回 None"""
        import uuid as uu
        fake_id = str(uu.uuid4())

        result = get_session_file(fake_id)
        assert result is None

    def test_sessions_dir_not_exists(self, temp_home):
        """SESSIONS_DIR 不存在时返回 None"""
        fake_sessions = temp_home / "fake_sessions"
        with patch('mycode.session.SESSIONS_DIR', fake_sessions):
            result = get_session_file("any-id")
            assert result is None


class TestFindLatestSessionFile:
    """测试 find_latest_session_file"""

    def test_returns_latest_by_mtime(self, temp_home):
        """按修改时间返回最新的会话文件"""
        sessions_dir = temp_home / "sessions"
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
            _history1 = SessionHistory("/test", model="test-model")
            import time
            time.sleep(0.1)
            history2 = SessionHistory("/test", model="test-model")

        with patch('mycode.session.SESSIONS_DIR', sessions_dir), patch('mycode.session.os.getcwd', return_value="/test"):
            result = find_latest_session_file()
        assert result is not None
        assert result == history2.file_path

    def test_single_session(self, temp_home):
        """只有一个会话时返回该会话"""
        sessions_dir = temp_home / "sessions"
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
            history = SessionHistory("/test", model="test-model")

        with patch('mycode.session.SESSIONS_DIR', sessions_dir), patch('mycode.session.os.getcwd', return_value="/test"):
            result = find_latest_session_file()
        assert result is not None
        assert result == history.file_path

    def test_no_sessions_for_cwd(self, temp_home):
        """当前目录没有会话时返回 None"""
        sessions_dir = temp_home / "sessions"
        with patch('mycode.session.SESSIONS_DIR', sessions_dir):
            # 在另一个目录创建会话
            SessionHistory("/other", model="test-model")

        # 当前目录是 /test，没有会话
        with patch('mycode.session.SESSIONS_DIR', sessions_dir), patch('mycode.session.os.getcwd', return_value="/test"):
            result = find_latest_session_file()
        assert result is None

    def test_sessions_dir_not_exists(self, temp_home):
        """SESSIONS_DIR 不存在时返回 None"""
        fake_sessions = temp_home / "fake_sessions"
        with patch('mycode.session.SESSIONS_DIR', fake_sessions):
            result = find_latest_session_file()
            assert result is None
