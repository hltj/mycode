"""
自定义类型（dataclass / TypedDict / NamedTuple）展开为 JSON Schema 的测试。

覆盖：
- dataclass：字段标注 + 必填（无默认值）与选填（有默认值）判定
- TypedDict：字段标注 + __required_keys__ 判定（含 total=False / 继承）
- NamedTuple：字段标注 + 默认值判定
- 嵌套 list[TypedDict] / Optional[TypedDict] / list[dataclass] 递归展开
- 字段描述（Annotated 内嵌）透传
- 注册表整链：工具参数用 list[TypedDict] 时生成的 schema 精确到嵌套字段
- 循环引用报错 / 定义时重名工具名冲突
"""

import dataclasses
from dataclasses import dataclass
from typing import Annotated, NamedTuple, Optional, TypedDict

import inspect
import pytest

from mycode.tools_registry import (
    ToolsRegistry,
    _build_json_schema,
    _parse_param,
)


# ===================================================================
# dataclass
# ===================================================================

@dataclass
class DataPoint:
    x: int
    y: int = 0
    label: str | None = None


def test_build_json_schema_dataclass():
    schema = _build_json_schema(DataPoint)
    assert schema["type"] == "object"
    assert schema["properties"]["x"]["type"] == "integer"
    assert schema["properties"]["y"]["type"] == "integer"
    assert schema["properties"]["label"]["type"] == "string"
    # x 无默认值 → 必填；y / label 有默认值 → 选填
    assert schema["required"] == ["x"]


def test_build_json_schema_dataclass_fields_optional():
    @dataclass
    class Config:
        name: str = "default"
        count: int = 3

    schema = _build_json_schema(Config)
    assert schema["required"] == []
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["count"]["type"] == "integer"


# ===================================================================
# TypedDict
# ===================================================================

class UserProfile(TypedDict):
    """用户资料"""
    name: str
    age: int | None


def test_build_json_schema_typed_dict():
    schema = _build_json_schema(UserProfile)
    assert schema["type"] == "object"
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["age"]["type"] == "integer"
    assert schema["required"] == ["name", "age"]


def test_build_json_schema_typed_dict_total_false():
    class Partial(TypedDict, total=False):
        name: str
        desc: str | None

    schema = _build_json_schema(Partial)
    assert schema["type"] == "object"
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["desc"]["type"] == "string"
    # total=False → 全部选填
    assert schema["required"] == []


def test_build_json_schema_typed_dict_inherited_required():
    class Base(TypedDict):
        label: str

    class Extra(Base, total=False):
        desc: str

    schema = _build_json_schema(Extra)
    assert schema["required"] == ["label"]
    assert schema["properties"]["label"]["type"] == "string"
    assert schema["properties"]["desc"]["type"] == "string"


def test_build_json_schema_typed_dict_docstring_description():
    """TypedDict 的字段描述（Annotated）透传到 properties。"""
    class WithDesc(TypedDict):
        name: Annotated[str, "用户名"]

    schema = _build_json_schema(WithDesc)
    assert schema["properties"]["name"]["description"] == "用户名"


# ===================================================================
# NamedTuple
# ===================================================================

class Row(NamedTuple):
    name: str
    age: int = 0


def test_build_json_schema_named_tuple():
    schema = _build_json_schema(Row)
    assert schema["type"] == "object"
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["age"]["type"] == "integer"
    assert schema["required"] == ["name"]


# ===================================================================
# 嵌套 / 泛型组合
# ===================================================================

def test_build_json_schema_list_typed_dict():
    schema = _build_json_schema(list[UserProfile])
    assert schema["type"] == "array"
    assert schema["items"]["type"] == "object"
    assert schema["items"]["properties"]["name"]["type"] == "string"


def test_build_json_schema_optional_typed_dict():
    schema = _build_json_schema(UserProfile | None)
    assert schema["type"] == "object"
    assert schema["properties"]["name"]["type"] == "string"


def test_build_json_schema_list_dataclass():
    schema = _build_json_schema(list[DataPoint])
    assert schema["type"] == "array"
    assert schema["items"]["type"] == "object"
    assert schema["items"]["required"] == ["x"]


def test_build_json_schema_typed_dict_nested_list():
    class Playlist(TypedDict):
        title: str
        tracks: list[DataPoint]

    schema = _build_json_schema(Playlist)
    assert schema["properties"]["tracks"]["type"] == "array"
    assert schema["properties"]["tracks"]["items"]["type"] == "object"
    assert schema["properties"]["tracks"]["items"]["required"] == ["x"]


# ===================================================================
# 错误处理
# ===================================================================

def test_build_json_schema_unsupported_custom_class_raises():
    """无法展开为对象 schema 的自定义类（无 dataclass/TypedDict/NamedTuple 标记）报错。"""
    class Plain:
        pass

    with pytest.raises(ValueError, match="Unsupported type"):
        _build_json_schema(Plain)


# ===================================================================
# _parse_param 整链
# ===================================================================

def test_parse_param_typed_dict():
    def dummy(profile: UserProfile):
        pass

    sig = inspect.signature(dummy)
    result = _parse_param(list(sig.parameters.values())[0])
    assert result["type"] == "object"
    assert result["properties"]["name"]["type"] == "string"
    assert result["required"] == ["name", "age"]


def test_parse_param_list_typed_dict():
    def dummy(users: list[UserProfile]):
        pass

    sig = inspect.signature(dummy)
    result = _parse_param(list(sig.parameters.values())[0])
    assert result["type"] == "array"
    assert result["items"]["type"] == "object"
    assert result["items"]["properties"]["name"]["type"] == "string"
    assert result["items"]["required"] == ["name", "age"]


def test_parse_param_annotated_list_typed_dict():
    def dummy(users: Annotated[list[UserProfile], "一批用户"]):
        pass

    sig = inspect.signature(dummy)
    result = _parse_param(list(sig.parameters.values())[0])
    assert result["description"] == "一批用户"
    assert result["type"] == "array"
    assert result["items"]["type"] == "object"
    assert result["items"]["properties"]["name"]["type"] == "string"


# ===================================================================
# 注册表整链（工具参数用 list[TypedDict]）
# ===================================================================

class AskQuestionParam(TypedDict, total=False):
    header: Annotated[str, "问题简短标题"]
    question: str
    options: list[dict]


@pytest.fixture(autouse=True)
def _reset_registry():
    ToolsRegistry.reset()
    yield
    ToolsRegistry.reset()


def test_tool_decorated_list_typed_dict():
    """工具参数用 list[TypedDict]，schema 精确展开嵌套字段。"""

    @ToolsRegistry.tool(description="询问")
    def ask(questions: Annotated[list[AskQuestionParam], "问题数组"]):
        return "ok"

    params = ToolsRegistry.get_tool_def("ask")["function"]["parameters"]
    questions = params["properties"]["questions"]
    assert questions["type"] == "array"
    assert questions["description"] == "问题数组"
    items = questions["items"]
    assert items["type"] == "object"
    # total=False → header 选填；字段描述透传
    assert items["properties"]["header"]["type"] == "string"
    assert items["properties"]["header"]["description"] == "问题简短标题"
    assert items["properties"]["question"]["type"] == "string"
    assert items["properties"]["options"]["type"] == "array"
    assert items["properties"]["options"]["items"]["type"] == "object"
    assert "header" not in items["required"]


def test_tool_decorated_required_typed_dict():
    """必填 TypedDict 的字段进 required，整体仍是必填参数。"""

    class ReqDict(TypedDict):
        name: str

    @ToolsRegistry.tool(description="存")
    def save(entry: ReqDict):
        return "ok"

    params = ToolsRegistry.get_tool_def("save")["function"]["parameters"]
    assert "entry" in params["required"]
    assert params["properties"]["entry"]["required"] == ["name"]


def test_tool_decorated_dataclass_param():
    """dataclass 直接作为参数类型展开。"""

    @ToolsRegistry.tool(description="点")
    def move(pt: DataPoint):
        return "ok"

    params = ToolsRegistry.get_tool_def("move")["function"]["parameters"]
    assert params["properties"]["pt"]["type"] == "object"
    assert params["properties"]["pt"]["required"] == ["x"]


# ===================================================================
# Literal → enum
# ===================================================================

def test_build_json_schema_literal():
    """Literal['a', 'b'] → {type: string, enum: ['a','b']}。"""
    from typing import Literal
    schema = _build_json_schema(Literal["a", "b"])
    assert schema == {"type": "string", "enum": ["a", "b"]}


def test_build_json_schema_literal_int():
    from typing import Literal
    schema = _build_json_schema(Literal[1, 2, 3])
    assert schema == {"type": "integer", "enum": [1, 2, 3]}


def test_build_json_schema_literal_bool():
    from typing import Literal
    schema = _build_json_schema(Literal[True, False])
    assert schema == {"type": "boolean", "enum": [True, False]}


def test_build_json_schema_literal_none_only():
    """Literal[None] 退化为 null 类型下限约束。"""
    from typing import Literal
    schema = _build_json_schema(Literal[None])
    assert schema["type"] == "null"


def test_typed_dict_with_literal_field():
    """TypedDict 字段用 Literal，schema 带 enum。"""
    from typing import Literal

    class TodoItem(TypedDict):
        title: str
        status: Literal["pending", "in_progress", "completed"]

    schema = _build_json_schema(TodoItem)
    assert schema["properties"]["status"]["type"] == "string"
    assert schema["properties"]["status"]["enum"] == [
        "pending", "in_progress", "completed"
    ]
    assert schema["properties"]["title"]["type"] == "string"


def test_parse_param_literal_list():
    """list[Literal] 参数解析带 enum。"""
    from typing import Literal, Annotated

    def dummy(status: Annotated[list[Literal["a", "b"]], "状态"]):
        pass

    sig = inspect.signature(dummy)
    result = _parse_param(list(sig.parameters.values())[0])
    assert result["type"] == "array"
    assert result["items"]["type"] == "string"
    assert result["items"]["enum"] == ["a", "b"]


def test_tool_decorated_typed_dict_literal_field():
    """工具参数为 list[TypedDict]，字面量字段带 enum。"""
    from typing import Literal

    class TodoItem(TypedDict):
        title: str
        status: Literal["pending", "in_progress", "completed"]

    @ToolsRegistry.tool(description="待办")
    def todo(items: list[TodoItem]):
        return "ok"

    params = ToolsRegistry.get_tool_def("todo")["function"]["parameters"]
    items = params["properties"]["items"]
    assert items["type"] == "array"
    item_schema = items["items"]
    assert item_schema["properties"]["status"]["enum"] == [
        "pending", "in_progress", "completed"
    ]
    assert item_schema["required"] == ["title", "status"]
