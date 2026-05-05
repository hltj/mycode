#!/usr/bin/env python3
"""
元编程工具定义模块，以函数式风格编写，直接返回 OpenAI SDK 原生类型。
使用 typing.Annotated 为参数提供可选描述。
"""
from __future__ import annotations

import inspect
import types
import typing
from typing import (
    NoReturn,
    Any, Callable, Union,
    get_args, get_origin, overload
)

from openai.types.chat import ChatCompletionFunctionToolParam
from openai.types.shared_params import FunctionDefinition


def _type_to_json_schema_type(
    py_type: type[int] | type[float] | type[str] | type[bool] | type[list] | type[dict]
) -> str:
    """Python 类型到 JSON Schema 类型的映射"""
    type_map = {
        int: "integer", float: "number", str: "string",
        bool: "boolean", list: "array", dict: "object",
    }
    if py_type not in type_map:
        raise ValueError(f"Unsupported type: {py_type}")
    return type_map[py_type]


# 用于 _origin_to_json_schema 和 _build_json_schema 中判断泛型 origin
_LIST_ORIGINS: tuple = (list, typing.List)
_DICT_ORIGINS: tuple = (dict, typing.Dict)


def _origin_to_json_schema(origin: Any) -> str:
    """从泛型 origin 推导 JSON Schema 顶层 type"""
    if origin in _LIST_ORIGINS:
        return "array"
    if origin in _DICT_ORIGINS:
        return "object"
    return _type_to_json_schema_type(origin)


def _build_json_schema(py_type: Any) -> dict[str, Any]:
    """
    递归构建任意类型对应的 JSON Schema 片段。
    - 标量：{"type": "integer"} 等
    - list[T]：{"type": "array", "items": <T 的 schema>}
    - dict[K, V]：{"type": "object", "additionalProperties": <V 的 schema>}
    - Union/Optional 出现在任意层级都会被递归解包
    """
    origin = get_origin(py_type)

    # 若出现 Union / Optional（含 | None），先解出非 None 部分再递归构建
    if origin in (Union, types.UnionType):
        args = get_args(py_type)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _build_json_schema(non_none[0])

    if origin is None:
        # 标量类型
        return {"type": _type_to_json_schema_type(py_type)}

    # 泛型类型
    json_type = _origin_to_json_schema(origin)
    args = get_args(py_type)
    schema: dict[str, Any] = {"type": json_type}

    if origin in _LIST_ORIGINS and args:
        schema["items"] = _build_json_schema(args[0])
    elif origin in _DICT_ORIGINS and args:
        # dict[K, V] -> additionalProperties 使用值类型 V
        schema["additionalProperties"] = _build_json_schema(args[1])

    return schema


def _parse_param(param: inspect.Parameter) -> dict[str, Any]:
    """解析单个参数，返回 JSON Schema 属性定义"""
    if param.annotation == inspect.Parameter.empty:
        raise TypeError(f"参数 \"{param.name}\" 缺少类型注解。所有参数必须提供类型注解。")
    annotation = param.annotation

    base_type = annotation
    description: str | None = None

    if get_origin(annotation) is typing.Annotated:
        args = get_args(annotation)
        if args:
            base_type = args[0]
            desc_parts = [a for a in args[1:] if isinstance(a, str)]
            if desc_parts:
                description = " ".join(desc_parts)

    result = _build_json_schema(base_type)
    if description is not None:
        result["description"] = description

    return result


def _extract_parameters(func: Callable[..., Any]) -> dict[str, Any]:
    """从函数签名中提取所有参数信息，生成 JSON Schema"""
    sig = inspect.signature(func)
    parameters = sig.parameters

    valid_params = [
        (name, param) for name, param in parameters.items()
        if param.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]

    properties = {name: _parse_param(param) for name, param in valid_params}
    required = [name for name, param in valid_params if param.default == inspect.Parameter.empty]

    return {"type": "object", "properties": properties, "required": required}


class ToolsRegistry:
    """工具注册表，纯类方法实现，无需实例化"""
    _tools: list[ChatCompletionFunctionToolParam] = []
    _handlers: dict[str, Callable] = {}

    def __new__(cls, *_: Any, **__: Any) -> NoReturn:
        raise TypeError(f"{cls.__name__} 禁止实例化，请直接调用类方法。")

    @classmethod
    def reset(cls) -> None:
        """重置注册表（主要用于测试）"""
        cls._tools.clear()
        cls._handlers.clear()

    @overload
    @classmethod
    def tool(
        cls,
        *,
        description: str | None = None,
        name: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...

    @overload
    @classmethod
    def tool(
        cls,
        _func: Callable[..., Any],
        *,
        description: str | None = None,
        name: str | None = None,
    ) -> Callable[..., Any]: ...

    @classmethod
    def tool(
        cls,
        _func: Callable[..., Any] | None = None,
        *,
        description: str | None = None,
        name: str | None = None,
    ) -> Callable[..., Any] | Callable[[Callable[..., Any]], Callable[..., Any]]:
        """
        装饰器方式注册工具。
        自动从函数签名生成 OpenAI SDK 原生的 ChatCompletionFunctionToolParam。

        用法:
            @registry.tool
            def my_func(x: int) -> str: ...

            @registry.tool(description="...", name="...")
            def my_func(x: int) -> str: ...

        参数描述通过 typing.Annotated[Type, "description"] 提供。
        """
        def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
            t_name = name or f.__name__
            t_desc = description or inspect.getdoc(f) or ""
            params = _extract_parameters(f)
            sdk_tool = ChatCompletionFunctionToolParam(
                type="function",
                function=FunctionDefinition(name=t_name, description=t_desc, parameters=params),
            )
            cls._handlers[t_name] = f
            cls._tools.append(sdk_tool)
            return f

        if _func is not None:
            return decorator(_func)
        return decorator

    @classmethod
    def get_tools(cls) -> list[ChatCompletionFunctionToolParam]:
        """获取所有工具，返回 OpenAI SDK 原生类型列表"""
        return cls._tools

    @classmethod
    def get_handler(cls, tool_name: str) -> Callable | None:
        """根据工具名查找处理函数（O(1) 字典查找）"""
        return cls._handlers.get(tool_name)

    @classmethod
    def get_tool_def(cls, tool_name: str) -> ChatCompletionFunctionToolParam | None:
        """根据工具名查找 SDK 工具定义"""
        return next(
            (t for t in cls._tools if t["function"]["name"] == tool_name),
            None,
        )


__all__ = ["ToolsRegistry"]
