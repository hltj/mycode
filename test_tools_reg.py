#!/usr/bin/env python3
"""
TDD tests for tools_reg.py
"""
import pytest
from typing import Annotated, Dict, List, Optional
from openai.types.chat import ChatCompletionFunctionToolParam
from openai.types.shared_params import FunctionDefinition
from tools_reg import ToolsRegistry, _parse_param, _type_to_json_schema_type
import inspect


@pytest.fixture(autouse=True)
def reset_registry():
    ToolsRegistry.reset()
    yield
    ToolsRegistry.reset()


def test_tool_decorator_basic():
    @ToolsRegistry.tool
    def add(a: int, b: int) -> int:
        """两个整数相加"""
        return a + b

    assert add(2, 3) == 5
    sdk_tool = ToolsRegistry.get_tool_def("add")
    assert sdk_tool is not None
    assert sdk_tool["function"]["name"] == "add"
    assert sdk_tool["function"]["description"] == "两个整数相加"
    # 校验实际数据
    assert sdk_tool["type"] == "function"
    assert sdk_tool["function"]["parameters"]["type"] == "object"
    assert sdk_tool["function"]["parameters"]["properties"]["a"]["type"] == "integer"
    assert sdk_tool["function"]["parameters"]["properties"]["b"]["type"] == "integer"
    assert "a" in sdk_tool["function"]["parameters"]["required"]
    assert "b" in sdk_tool["function"]["parameters"]["required"]


def test_tool_decorator_description_override():
    @ToolsRegistry.tool(description="自定义描述")
    def my_func(x: str) -> str:
        return x

    sdk_tool = ToolsRegistry.get_tool_def("my_func")
    assert isinstance(sdk_tool, dict)
    assert sdk_tool["function"]["description"] == "自定义描述"
    assert sdk_tool["function"]["name"] == "my_func"
    assert sdk_tool["type"] == "function"
    assert sdk_tool["function"]["parameters"]["properties"]["x"]["type"] == "string"
    assert "x" in sdk_tool["function"]["parameters"]["required"]


def test_tool_decorator_parameters():
    @ToolsRegistry.tool
    def greet(name: str, greeting: str = "你好") -> str:
        return f"{greeting}, {name}!"

    sdk_tool = ToolsRegistry.get_tool_def("greet")
    params = sdk_tool["function"]["parameters"]
    assert params["type"] == "object"
    assert "name" in params["properties"]
    assert "greeting" in params["properties"]
    assert params["properties"]["name"]["type"] == "string"
    assert params["properties"]["greeting"]["type"] == "string"
    assert "name" in params["required"]
    assert "greeting" not in params["required"]


def test_type_mapping():
    @ToolsRegistry.tool
    def multi_types(a: int, b: float, c: str, d: bool, e: list, f: dict) -> str:
        return "ok"

    props = ToolsRegistry.get_tool_def("multi_types")["function"]["parameters"]["properties"]
    assert props["a"]["type"] == "integer"
    assert props["b"]["type"] == "number"
    assert props["c"]["type"] == "string"
    assert props["d"]["type"] == "boolean"
    assert props["e"]["type"] == "array"
    assert props["f"]["type"] == "object"


def test_optional_parameter():
    @ToolsRegistry.tool
    def with_optional(req: str, opt: str | None = None) -> str:
        return req

    params = ToolsRegistry.get_tool_def("with_optional")["function"]["parameters"]
    assert "req" in params["required"]
    assert "opt" not in params["required"]
    assert params["properties"]["opt"]["type"] == "string"


def test_parameter_description_annotated():
    @ToolsRegistry.tool
    def create_user(name: Annotated[str, "用户名，3-20个字符"], age: Annotated[int, "年龄，1-150"]) -> str:
        return f"{name}, {age}"

    params = ToolsRegistry.get_tool_def("create_user")["function"]["parameters"]
    assert params["properties"]["name"]["description"] == "用户名，3-20个字符"
    assert params["properties"]["age"]["description"] == "年龄，1-150"
    assert params["properties"]["name"]["type"] == "string"
    assert params["properties"]["age"]["type"] == "integer"


def test_parameter_description_partial():
    @ToolsRegistry.tool
    def partial_desc(a: Annotated[int, "第一个数"], b: int) -> int:
        return a + b

    params = ToolsRegistry.get_tool_def("partial_desc")["function"]["parameters"]
    assert params["properties"]["a"]["description"] == "第一个数"
    assert "description" not in params["properties"]["b"]
    assert params["properties"]["a"]["type"] == "integer"
    assert params["properties"]["b"]["type"] == "integer"


def test_to_chat_completion_function_tool_param_type():
    @ToolsRegistry.tool(description="运行 bash 命令")
    def bash(command: Annotated[str, "要执行的 bash 命令"]) -> str:
        return "ok"

    sdk_tool = ToolsRegistry.get_tool_def("bash")
    expected = ChatCompletionFunctionToolParam(
        type='function',
        function=FunctionDefinition(name='x', description='y', parameters={'type': 'object'})
    )
    # 类型校验
    assert type(sdk_tool) is type(expected)
    # 数据校验
    assert sdk_tool["type"] == "function"
    assert sdk_tool["function"]["name"] == "bash"
    assert sdk_tool["function"]["description"] == "运行 bash 命令"
    assert sdk_tool["function"]["parameters"]["type"] == "object"
    assert sdk_tool["function"]["parameters"]["properties"]["command"]["type"] == "string"
    assert sdk_tool["function"]["parameters"]["properties"]["command"]["description"] == "要执行的 bash 命令"
    assert "command" in sdk_tool["function"]["parameters"]["required"]


def test_function_is_function_definition_type():
    @ToolsRegistry.tool(description="测试")
    def mytool(x: Annotated[int, "参数x"]) -> str:
        return str(x)

    sdk_tool = ToolsRegistry.get_tool_def("mytool")
    expected_func = FunctionDefinition(name='x', description='y', parameters={'type': 'object'})
    # 类型校验
    assert type(sdk_tool["function"]) is type(expected_func)
    # 数据校验
    assert sdk_tool["function"]["name"] == "mytool"
    assert sdk_tool["function"]["description"] == "测试"
    assert sdk_tool["function"]["parameters"]["properties"]["x"]["type"] == "integer"
    assert sdk_tool["function"]["parameters"]["properties"]["x"]["description"] == "参数x"


def test_bash_tool_equivalent():
    @ToolsRegistry.tool(description="运行 bash 命令")
    def bash(command: Annotated[str, "要执行的 bash 命令"]) -> str:
        return ""

    sdk_tool = ToolsRegistry.get_tool_def("bash")
    manual = ChatCompletionFunctionToolParam(
        type='function',
        function=FunctionDefinition(
            name="bash",
            description="运行 bash 命令",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string", "description": "要执行的 bash 命令"}},
                "required": ["command"],
            },
        ),
    )
    # 类型校验
    assert type(sdk_tool) is type(manual)
    assert type(sdk_tool["function"]) is type(manual["function"])
    # 数据校验
    assert sdk_tool == manual
    assert sdk_tool["type"] == "function"
    assert sdk_tool["function"]["name"] == "bash"
    assert sdk_tool["function"]["description"] == "运行 bash 命令"
    assert sdk_tool["function"]["parameters"]["properties"]["command"]["type"] == "string"


def test_registry_tools():
    @ToolsRegistry.tool(description="加法")
    def add(a: int, b: int) -> int:
        return a + b

    @ToolsRegistry.tool(description="乘法")
    def multiply(a: int, b: int) -> int:
        return a * b

    tools = ToolsRegistry.get_tools()
    assert len(tools) == 2
    assert "add" in [t["function"]["name"] for t in tools]
    assert "multiply" in [t["function"]["name"] for t in tools]
    # 校验具体数据
    add_tool = next(t for t in tools if t["function"]["name"] == "add")
    assert add_tool["function"]["description"] == "加法"
    assert add_tool["type"] == "function"
    multiply_tool = next(t for t in tools if t["function"]["name"] == "multiply")
    assert multiply_tool["function"]["description"] == "乘法"
    assert multiply_tool["type"] == "function"


def test_registry_class_based():
    @ToolsRegistry.tool(description="独立测试")
    def f(x: int) -> int:
        return x

    assert len(ToolsRegistry.get_tools()) == 1
    assert ToolsRegistry.get_handler("f") is not None
    assert len(ToolsRegistry.get_tools()) == 1
    handler = ToolsRegistry.get_handler("f")
    assert handler(42) == 42
    tool_def = ToolsRegistry.get_tool_def("f")
    assert tool_def["function"]["name"] == "f"
    assert tool_def["function"]["description"] == "独立测试"


def test_registry_get_tools_returns_typed_list():
    @ToolsRegistry.tool(description="加法")
    def add(a: int, b: int) -> int:
        return a + b

    tools = ToolsRegistry.get_tools()
    assert len(tools) == 1
    expected = ChatCompletionFunctionToolParam(
        type='function',
        function=FunctionDefinition(name='x', description='y', parameters={'type': 'object'})
    )
    # 类型校验
    assert type(tools[0]) is type(expected)
    # 数据校验
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "add"
    assert tools[0]["function"]["description"] == "加法"
    assert tools[0]["function"]["parameters"]["properties"]["a"]["type"] == "integer"
    assert tools[0]["function"]["parameters"]["properties"]["b"]["type"] == "integer"


def test_get_handler_from_registry():
    @ToolsRegistry.tool(description="查询天气")
    def get_weather(city: str) -> str:
        return f"{city}的阳光明媚"

    handler = ToolsRegistry.get_handler("get_weather")
    assert handler is not None
    assert handler("北京") == "北京的阳光明媚"
    assert ToolsRegistry.get_handler("nonexistent") is None


def test_tool_can_be_used_directly_with_openai_sdk():
    @ToolsRegistry.tool(description="计算两个数的和")
    def add(a: Annotated[int, "第一个数"], b: Annotated[int, "第二个数"]) -> int:
        return a + b

    sdk_tool = ToolsRegistry.get_tool_def("add")

    typed: ChatCompletionFunctionToolParam = sdk_tool  # type: ignore[assignment]
    assert typed["function"]["name"] == "add"
    # 数据校验
    assert sdk_tool["function"]["description"] == "计算两个数的和"
    assert sdk_tool["function"]["parameters"]["properties"]["a"]["type"] == "integer"
    assert sdk_tool["function"]["parameters"]["properties"]["b"]["type"] == "integer"
    assert sdk_tool["function"]["parameters"]["properties"]["a"]["description"] == "第一个数"
    assert sdk_tool["function"]["parameters"]["properties"]["b"]["description"] == "第二个数"


def test_registry_tools_can_be_passed_to_sdk():
    @ToolsRegistry.tool(description="工具A")
    def tool_a(x: int) -> int:
        return x

    @ToolsRegistry.tool(description="工具B")
    def tool_b(y: str) -> str:
        return y

    # 类型校验：列表元素类型
    sdk_tools: list[ChatCompletionFunctionToolParam] = ToolsRegistry.get_tools()
    assert len(sdk_tools) == 2
    # 数据校验
    for t in sdk_tools:
        assert "function" in t and "type" in t
    tool_a_def = next(t for t in sdk_tools if t["function"]["name"] == "tool_a")
    assert tool_a_def["function"]["description"] == "工具A"
    assert tool_a_def["type"] == "function"
    tool_b_def = next(t for t in sdk_tools if t["function"]["name"] == "tool_b")
    assert tool_b_def["function"]["description"] == "工具B"
    assert tool_b_def["type"] == "function"


@pytest.mark.parametrize("py_type", [bytes, type("CustomType", (), {})])
def test_unmapped_type_raises_exception(py_type):
    with pytest.raises(ValueError, match="Unsupported type"):
        _type_to_json_schema_type(py_type)


def test_optional_type_is_unwrapped():
    def dummy(opt: str | None = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "string"


def test_missing_annotation_raises_error():
    with pytest.raises(TypeError, match="缺少类型注解"):
        @ToolsRegistry.tool
        def bad_func(x):  # 故意缺少类型注解
            return x


def test_parse_param_no_annotation_raises_error():
    """直接测试 _parse_param 在没有注解的情况下抛出 TypeError"""
    def no_annotation_func(x):
        return x

    sig = inspect.signature(no_annotation_func)
    param = list(sig.parameters.values())[0]
    assert param.annotation == inspect.Parameter.empty

    with pytest.raises(TypeError, match='参数 "x" 缺少类型注解'):
        _parse_param(param)


def test_parse_param_with_valid_annotation():
    """直接测试 _parse_param 在有注解的情况下正常工作"""
    def simple_func(x: int):
        return x

    sig = inspect.signature(simple_func)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result == {"type": "integer"}


def test_parse_param_with_annotated_description():
    """直接测试 _parse_param 处理 Annotated 类型和描述"""
    def annotated_func(name: Annotated[str, "用户名称"]) -> str:
        return name

    sig = inspect.signature(annotated_func)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "string"
    assert result["description"] == "用户名称"


def test_optional_pipe_syntax_unwrapped():
    """测试 Python 3.10+ 的 T | None 语法被正确解包"""
    def dummy(opt: int | None = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "integer"


def test_optional_pipe_syntax_type_str():
    """测试 str | None 被解包为 string"""
    def dummy(opt: str | None = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "string"


def test_build_json_schema_unwraps_union_types():
    """测试 _build_json_schema 统一处理 T | None 的解包"""
    from tools_reg import _build_json_schema
    t = int | None
    schema = _build_json_schema(t)
    assert schema == {"type": "integer"}


def test_tool_decorator_optional_pipe_syntax():
    """测试装饰器中 int | None 参数正确生成 JSON Schema"""
    @ToolsRegistry.tool
    def pipe_opt(req: str, opt: int | None = None) -> str:
        return req

    params = ToolsRegistry.get_tool_def("pipe_opt")["function"]["parameters"]
    assert params["properties"]["opt"]["type"] == "integer"
    assert "opt" not in params["required"]


def test_tool_custom_name():
    """测试 name 参数自定义工具名"""
    @ToolsRegistry.tool(name="custom_name")
    def original_name(x: int) -> int:
        """某个描述"""
        return x

    sdk_tool = ToolsRegistry.get_tool_def("custom_name")
    assert sdk_tool is not None
    assert sdk_tool["function"]["name"] == "custom_name"
    assert sdk_tool["function"]["description"] == "某个描述"
    assert ToolsRegistry.get_handler("custom_name") is original_name
    # 原始函数名不应出现在注册表中
    assert ToolsRegistry.get_tool_def("original_name") is None


def test_tool_no_docstring_no_description():
    """测试函数无 docstring 且未提供 description 时的行为：description 应为空字符串"""
    @ToolsRegistry.tool
    def no_doc_no_desc(x: int) -> int:
        return x

    sdk_tool = ToolsRegistry.get_tool_def("no_doc_no_desc")
    assert sdk_tool is not None
    assert sdk_tool["function"]["description"] == ""
    assert sdk_tool["function"]["name"] == "no_doc_no_desc"


def test_var_positional_and_var_keyword_skipped():
    """测试 *args / **kwargs 参数被正确跳过（required 中不含它们，properties 中也不含）"""
    @ToolsRegistry.tool
    def with_varargs(req: int, *args, **kwargs) -> int:
        return req

    params = ToolsRegistry.get_tool_def("with_varargs")["function"]["parameters"]
    # properties 中不应有 args/kwargs
    assert "args" not in params["properties"]
    assert "kwargs" not in params["properties"]
    # required 中不应有 args/kwargs
    assert "args" not in params["required"]
    assert "kwargs" not in params["required"]
    # req 应在 required 中且在 properties 中
    assert "req" in params["required"]
    assert "req" in params["properties"]
    assert params["properties"]["req"]["type"] == "integer"


# ======== Generic Type Tests ========

def test_list_str_generic():
    """测试 list[str] 泛型参数解析"""
    def dummy(items: list[str]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "string"


def test_list_int_generic():
    """测试 list[int] 泛型参数解析"""
    def dummy(items: list[int]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "integer"


def test_dict_str_int_generic():
    """测试 dict[str, int] 泛型参数解析"""
    def dummy(mapping: dict[str, int]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "integer"


def test_dict_str_str_generic():
    """测试 dict[str, str] 泛型参数解析"""
    def dummy(mapping: dict[str, str]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "string"


def test_dict_str_bool_generic():
    """测试 dict[str, bool] 泛型参数解析"""
    def dummy(flags: dict[str, bool]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "boolean"


def test_nested_list_list_str():
    """测试嵌套泛型 list[list[str]]"""
    def dummy(matrix: list[list[str]]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "array"
    assert result["items"]["items"]["type"] == "string"


def test_nested_dict_str_list_int():
    """测试嵌套泛型 dict[str, list[int]]"""
    def dummy(data: dict[str, list[int]]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "array"
    assert result["additionalProperties"]["items"]["type"] == "integer"


def test_optional_list_str():
    """测试 list[str] | None 被正确解包"""
    def dummy(items: list[str] | None = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "string"


def test_optional_dict_str_int():
    """测试 dict[str, int] | None 被正确解包"""
    def dummy(mapping: dict[str, int] | None = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "integer"


def test_list_float_generic():
    """测试 list[float] 泛型参数解析"""
    def dummy(numbers: list[float]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "number"


def test_list_bool_generic():
    """测试 list[bool] 泛型参数解析"""
    def dummy(flags: list[bool]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "boolean"


def test_tool_decorated_list_str():
    """测试装饰器中 list[str] 参数的 JSON Schema 生成"""
    @ToolsRegistry.tool
    def process_items(items: Annotated[list[str], "字符串列表"]) -> str:
        return ",".join(items)

    params = ToolsRegistry.get_tool_def("process_items")["function"]["parameters"]
    assert params["properties"]["items"]["type"] == "array"
    assert params["properties"]["items"]["items"]["type"] == "string"
    assert params["properties"]["items"]["description"] == "字符串列表"


def test_tool_decorated_dict_str_int():
    """测试装饰器中 dict[str, int] 参数的 JSON Schema 生成"""
    @ToolsRegistry.tool
    def process_scores(scores: Annotated[dict[str, int], "学生成绩"]) -> int:
        return sum(scores.values())

    params = ToolsRegistry.get_tool_def("process_scores")["function"]["parameters"]
    assert params["properties"]["scores"]["type"] == "object"
    assert params["properties"]["scores"]["additionalProperties"]["type"] == "integer"
    assert params["properties"]["scores"]["description"] == "学生成绩"


def test_tool_decorated_nested_generic():
    """测试装饰器中嵌套泛型的 JSON Schema 生成"""
    @ToolsRegistry.tool
    def process_matrix(data: list[list[int]]) -> int:
        return sum(sum(row) for row in data)

    params = ToolsRegistry.get_tool_def("process_matrix")["function"]["parameters"]
    assert params["properties"]["data"]["type"] == "array"
    assert params["properties"]["data"]["items"]["type"] == "array"
    assert params["properties"]["data"]["items"]["items"]["type"] == "integer"


def test_tool_decorated_optional_generic():
    """测试装饰器中 list[str] | None 的 JSON Schema 生成"""
    @ToolsRegistry.tool
    def process_optional(items: list[str] | None = None) -> str:
        return ",".join(items) if items else ""

    params = ToolsRegistry.get_tool_def("process_optional")["function"]["parameters"]
    assert params["properties"]["items"]["type"] == "array"
    assert params["properties"]["items"]["items"]["type"] == "string"
    assert "items" not in params["required"]


def test_pipe_optional_list_int():
    """测试 list[int] | None 语法"""
    @ToolsRegistry.tool
    def pipe_generic(req: str, nums: list[int] | None = None) -> str:
        return req

    params = ToolsRegistry.get_tool_def("pipe_generic")["function"]["parameters"]
    assert params["properties"]["nums"]["type"] == "array"
    assert params["properties"]["nums"]["items"]["type"] == "integer"
    assert "nums" not in params["required"]


def test_typing_list_and_dict():
    """测试 typing.List[str] 和 typing.Dict[str, int] 也生效"""
    from typing import List, Dict

    def dummy(a: List[str], b: Dict[str, int]):
        pass

    sig = inspect.signature(dummy)
    for name, param in sig.parameters.items():
        result = _parse_param(param)
        if name == "a":
            assert result["type"] == "array"
            assert result["items"]["type"] == "string"
        elif name == "b":
            assert result["type"] == "object"
            assert result["additionalProperties"]["type"] == "integer"


def test_deeply_nested_generic():
    """测试深度嵌套: dict[str, list[dict[str, list[float]]]]"""
    def dummy(data: dict[str, list[dict[str, list[float]]]]):
        pass

    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)

    # dict[str, ...]
    assert result["type"] == "object"
    # additionalProperties -> list[...]
    assert result["additionalProperties"]["type"] == "array"
    # items -> dict[str, ...]
    assert result["additionalProperties"]["items"]["type"] == "object"
    # additionalProperties -> list[float]
    assert result["additionalProperties"]["items"]["additionalProperties"]["type"] == "array"
    # items -> float
    assert result["additionalProperties"]["items"]["additionalProperties"]["items"]["type"] == "number"


# ======== Old Syntax (typing module) Compatibility Tests ========

def test_optional_old_syntax():
    """测试旧语法 Optional[str] 被正确解包"""
    def dummy(opt: Optional[str] = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "string"


def test_optional_list_str_old_syntax():
    """测试旧语法 Optional[list[str]] 被正确解包"""
    def dummy(items: Optional[list[str]] = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "string"


def test_optional_dict_str_int_old_syntax():
    """测试旧语法 Optional[dict[str, int]] 被正确解包"""
    def dummy(mapping: Optional[dict[str, int]] = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "integer"


def test_optional_parameter_old_syntax():
    """测试旧语法 Optional 装饰器场景"""
    @ToolsRegistry.tool
    def with_optional_old(req: str, opt: Optional[str] = None) -> str:
        return req

    params = ToolsRegistry.get_tool_def("with_optional_old")["function"]["parameters"]
    assert "req" in params["required"]
    assert "opt" not in params["required"]
    assert params["properties"]["opt"]["type"] == "string"


def test_decorated_optional_generic_old_syntax():
    """测试旧语法 Optional[list[str]] 装饰器场景"""
    @ToolsRegistry.tool
    def process_optional_old(items: Optional[list[str]] = None) -> str:
        return ",".join(items) if items else ""

    params = ToolsRegistry.get_tool_def("process_optional_old")["function"]["parameters"]
    assert params["properties"]["items"]["type"] == "array"
    assert params["properties"]["items"]["items"]["type"] == "string"
    assert "items" not in params["required"]


# ======== list/Dict 旧语法兼容性测试 ========

def test_list_str_old_syntax():
    """测试旧语法 List[str] 被正确解析"""
    def dummy(items: List[str]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "string"


def test_list_int_old_syntax():
    """测试旧语法 List[int] 被正确解析"""
    def dummy(items: List[int]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "integer"


def test_nested_list_old_syntax():
    """测试旧语法嵌套 List[List[str]]"""
    def dummy(matrix: List[List[str]]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "array"
    assert result["items"]["items"]["type"] == "string"


def test_dict_str_str_old_syntax():
    """测试旧语法 Dict[str, str] 被正确解析"""
    def dummy(mapping: Dict[str, str]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "string"


def test_nested_dict_list_old_syntax():
    """测试旧语法嵌套 Dict[str, List[int]]"""
    def dummy(data: Dict[str, List[int]]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "array"
    assert result["additionalProperties"]["items"]["type"] == "integer"


def test_tool_decorated_list_str_old_syntax():
    """测试旧语法 List[str] 装饰器场景"""
    @ToolsRegistry.tool
    def process_items_old(items: Annotated[List[str], "字符串列表"]) -> str:
        return ",".join(items)

    params = ToolsRegistry.get_tool_def("process_items_old")["function"]["parameters"]
    assert params["properties"]["items"]["type"] == "array"
    assert params["properties"]["items"]["items"]["type"] == "string"
    assert params["properties"]["items"]["description"] == "字符串列表"


def test_tool_decorated_dict_str_int_old_syntax():
    """测试旧语法 Dict[str, int] 装饰器场景"""
    @ToolsRegistry.tool
    def process_scores_old(scores: Annotated[Dict[str, int], "学生成绩"]) -> int:
        return sum(scores.values())

    params = ToolsRegistry.get_tool_def("process_scores_old")["function"]["parameters"]
    assert params["properties"]["scores"]["type"] == "object"
    assert params["properties"]["scores"]["additionalProperties"]["type"] == "integer"
    assert params["properties"]["scores"]["description"] == "学生成绩"


# ======== Optional 内嵌套旧语法 List/Dict 测试 ========

def test_optional_list_str_old_syntax_nested():
    """测试旧语法 Optional[List[str]] 被正确解包"""
    def dummy(items: Optional[List[str]] = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "string"


def test_optional_dict_str_int_old_syntax_nested():
    """测试旧语法 Optional[Dict[str, int]] 被正确解包"""
    def dummy(mapping: Optional[Dict[str, int]] = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "integer"


def test_optional_nested_list_old_syntax():
    """测试旧语法 Optional[List[List[float]]] 被正确解包"""
    def dummy(data: Optional[List[List[float]]] = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "array"
    assert result["items"]["items"]["type"] == "number"


def test_optional_nested_dict_old_syntax():
    """测试旧语法 Optional[Dict[str, List[int]]] 被正确解包"""
    def dummy(data: Optional[Dict[str, List[int]]] = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "array"
    assert result["additionalProperties"]["items"]["type"] == "integer"


def test_tool_decorated_optional_list_str_old_syntax():
    """测试旧语法 Optional[List[str]] 装饰器场景"""
    @ToolsRegistry.tool
    def process_optional_old(items: Optional[List[str]] = None) -> str:
        return ",".join(items) if items else ""

    params = ToolsRegistry.get_tool_def("process_optional_old")["function"]["parameters"]
    assert params["properties"]["items"]["type"] == "array"
    assert params["properties"]["items"]["items"]["type"] == "string"
    assert "items" not in params["required"]


def test_tool_decorated_optional_dict_str_int_old_syntax():
    """测试旧语法 Optional[Dict[str, int]] 装饰器场景"""
    @ToolsRegistry.tool
    def process_scores_old(scores: Optional[Dict[str, int]] = None) -> int:
        return sum(scores.values()) if scores else 0

    params = ToolsRegistry.get_tool_def("process_scores_old")["function"]["parameters"]
    assert params["properties"]["scores"]["type"] == "object"
    assert params["properties"]["scores"]["additionalProperties"]["type"] == "integer"
    assert "scores" not in params["required"]


def test_deeply_nested_optional_old_syntax():
    """测试旧语法 Optional[Dict[str, List[Dict[str, int]]]] 深度嵌套"""
    def dummy(data: Optional[Dict[str, List[Dict[str, int]]]] = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "array"
    assert result["additionalProperties"]["items"]["type"] == "object"
    assert result["additionalProperties"]["items"]["additionalProperties"]["type"] == "integer"


# ======== 新旧语法混搭场景测试 ========

def test_list_new_wrap_list_old():
    """测试新 list 包旧 List: list[List[str]]"""
    def dummy(data: list[List[str]]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "array"
    assert result["items"]["items"]["type"] == "string"


def test_list_old_wrap_list_new():
    """测试旧 List 包新 list: List[list[int]]"""
    def dummy(data: List[list[int]]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "array"
    assert result["items"]["items"]["type"] == "integer"


def test_dict_new_wrap_dict_old():
    """测试新 dict 包旧 Dict: dict[str, Dict[str, int]]"""
    def dummy(data: dict[str, Dict[str, int]]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "object"
    assert result["additionalProperties"]["additionalProperties"]["type"] == "integer"


def test_dict_old_wrap_dict_new():
    """测试旧 Dict 包新 dict: Dict[str, dict[str, float]]"""
    def dummy(data: Dict[str, dict[str, float]]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "object"
    assert result["additionalProperties"]["additionalProperties"]["type"] == "number"


def test_list_old_pipe_none():
    """测试旧 List 用新 | None 语法: List[str] | None"""
    def dummy(items: List[str] | None = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "string"


def test_dict_new_pipe_none():
    """测试新 dict 用 | None 被正确解包: dict[str, int] | None"""
    def dummy(mapping: dict[str, int] | None = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "integer"


def test_mixed_nested_deep():
    """测试深度混搭: Dict[str, list[Dict[str, List[float]]]] | None"""
    def dummy(data: Dict[str, list[Dict[str, List[float]]]] | None = None):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    # 外层 Dict[str, ...] -> object
    assert result["type"] == "object"
    # additionalProperties -> list[...] -> array
    assert result["additionalProperties"]["type"] == "array"
    # items -> Dict[str, ...] -> object
    assert result["additionalProperties"]["items"]["type"] == "object"
    # additionalProperties -> List[float] -> array
    assert result["additionalProperties"]["items"]["additionalProperties"]["type"] == "array"
    # items -> float -> number
    assert result["additionalProperties"]["items"]["additionalProperties"]["items"]["type"] == "number"


def test_tool_decorated_list_old_pipe_none():
    """测试装饰器中 List[str] | None"""
    @ToolsRegistry.tool
    def tool_list_old_pipe_none(items: List[str] | None = None) -> str:
        return ",".join(items) if items else ""

    params = ToolsRegistry.get_tool_def("tool_list_old_pipe_none")["function"]["parameters"]
    assert params["properties"]["items"]["type"] == "array"
    assert params["properties"]["items"]["items"]["type"] == "string"
    assert "items" not in params["required"]


def test_tool_decorated_mixed_dict_list():
    """测试装饰器中 dict[str, List[int]] | None"""
    @ToolsRegistry.tool
    def tool_mixed(data: dict[str, List[int]] | None = None) -> int:
        return sum(sum(v for v in data.values()) for v in data.values()) if data else 0

    params = ToolsRegistry.get_tool_def("tool_mixed")["function"]["parameters"]
    assert params["properties"]["data"]["type"] == "object"
    assert params["properties"]["data"]["additionalProperties"]["type"] == "array"
    assert params["properties"]["data"]["additionalProperties"]["items"]["type"] == "integer"
    assert "data" not in params["required"]


# ======== 内部 Optional / | None 递归解包测试（TDD：先写测试，再实现） ========

def test_parse_param_list_optional_str_new():
    """测试 _parse_param(list[Optional[str]]) 内部 Optional 被递归解包"""
    def dummy(items: list[Optional[str]]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "string"


def test_parse_param_list_optional_int_new():
    """测试 _parse_param(list[Optional[int]]) 内部 Optional 被递归解包"""
    def dummy(items: list[Optional[int]]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "integer"


def test_parse_param_list_optional_with_annotated():
    """测试 Annotated[list[Optional[str]], '描述'] 中内部 Optional 被解包"""
    def dummy(items: Annotated[list[Optional[str]], "可空字符串列表"]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "string"
    assert result["description"] == "可空字符串列表"


def test_parse_param_list_optional_pipe_none():
    """测试 list[str | None] 内部 union 被解包"""
    def dummy(items: list[str | None]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "string"


def test_parse_param_dict_optional_value_new():
    """测试 dict[str, Optional[int]] 内部 Optional 被解包"""
    def dummy(data: dict[str, Optional[int]]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "integer"


def test_parse_param_dict_optional_value_pipe_none():
    """测试 dict[str, int | None] 内部 union 被解包"""
    def dummy(data: dict[str, int | None]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "integer"


def test_parse_param_list_nested_optional_old():
    """测试 List[List[Optional[float]]] 多层嵌套中的 Optional 被解包"""
    def dummy(data: List[List[Optional[float]]]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "array"
    assert result["items"]["type"] == "array"
    assert result["items"]["items"]["type"] == "number"


def test_parse_param_dict_nested_optional_mix():
    """测试 Dict[str, list[Optional[str]] | None] 混搭中内部 Optional 和 | None 都被解包"""
    def dummy(data: Dict[str, list[Optional[str]] | None]):
        pass
    sig = inspect.signature(dummy)
    param = list(sig.parameters.values())[0]
    result = _parse_param(param)
    assert result["type"] == "object"
    assert result["additionalProperties"]["type"] == "array"
    assert result["additionalProperties"]["items"]["type"] == "string"


def test_tool_decorated_internal_optional():
    """测试装饰器中 list[Optional[str]] 正确生成 JSON Schema"""
    @ToolsRegistry.tool
    def with_internal_optional(items: Optional[list[Optional[str]]] = None) -> str:
        return ",".join(items) if items else ""

    params = ToolsRegistry.get_tool_def("with_internal_optional")["function"]["parameters"]
    assert params["properties"]["items"]["type"] == "array"
    assert params["properties"]["items"]["items"]["type"] == "string"
    assert "items" not in params["required"]


def test_tool_decorated_internal_pipe_none():
    """测试装饰器中 dict[str, int | None] 正确生成 JSON Schema"""
    @ToolsRegistry.tool
    def with_internal_pipe_none(data: Optional[dict[str, int | None]] = None) -> int:
        return sum(v for v in data.values() if v) if data else 0

    params = ToolsRegistry.get_tool_def("with_internal_pipe_none")["function"]["parameters"]
    assert params["properties"]["data"]["type"] == "object"
    assert params["properties"]["data"]["additionalProperties"]["type"] == "integer"
    assert "data" not in params["required"]


def test_tools_registry_cannot_be_instantiated():
    """测试 ToolsRegistry 无法被实例化，调用应抛出 TypeError"""
    with pytest.raises(TypeError, match="ToolsRegistry 禁止实例化"):
        ToolsRegistry()
