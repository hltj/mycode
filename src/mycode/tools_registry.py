"""
元编程工具定义模块，以函数式风格编写，直接返回 OpenAI SDK 原生类型。
使用 typing.Annotated 为参数提供可选描述。
"""
from __future__ import annotations

import dataclasses
import inspect
import types
import typing
from typing import (
    NoReturn,
    Any, Callable, Union, cast,
    get_args, get_origin, get_type_hints, overload
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


def _literal_schema(values: tuple[Any, ...]) -> dict[str, Any]:
    """Literal[v1, v2, ...] → {type, enum}。

    - 所有枚举值同类型：取该类型的 JSON 类型 + enum；
    - 含 None 枚举：只取非 None 值，type 用非 None 值类型，
      None 单独以 null 记入 enum（与 OpenAI 工具 schema 兼容）。
    """
    non_none = [v for v in values if v is not None]
    if not non_none:
        return {"type": "null"}
    # 枚举值类型须一致（Literal 保证），取第一个值的类型
    json_type = _type_to_json_schema_type(type(non_none[0]))
    return {"type": json_type, "enum": list(values)}


def _origin_to_json_schema(origin: Any) -> str:
    """从泛型 origin 推导 JSON Schema 顶层 type"""
    if origin in _LIST_ORIGINS:
        return "array"
    if origin in _DICT_ORIGINS:
        return "object"
    return _type_to_json_schema_type(origin)


# ---------------------------------------------------------------------------
# 自定义类型的对象 schema（dataclass / TypedDict / NamedTuple）
# ---------------------------------------------------------------------------

def _parse_type_with_meta(annotation: Any) -> dict[str, Any]:
    """解析一个类型标注（可含 Annotated 元信息），返回 JSON Schema 片段。

    ``Annotated[T, '描述']``：透传 description。
    """
    if get_origin(annotation) is typing.Annotated:
        args = get_args(annotation)
        if args:
            desc_parts = [a for a in args[1:] if isinstance(a, str)]
            schema = _build_json_schema(args[0])
            if desc_parts:
                schema["description"] = " ".join(desc_parts)
            return schema
    return _build_json_schema(annotation)


def _resolve_field_annotations(
    cls: type,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """把类的字段原始标注解析为可构建 schema 的实用类型。

    - 字符串标注（``from __future__ import annotations``）经
      ``get_type_hints`` 求值成实际类型；
    - ``Annotated[T, '描述']`` 原样保留（描述透传给 schema）。
    """
    hints = get_type_hints(cls)
    return {
        name: (hints.get(name, ann) if isinstance(ann, str) else ann)
        for name, ann in raw.items()
    }


def _typed_dict_annotations(td: Any) -> dict[str, Any]:
    """合并 TypedDict 继承链上的字段标注（保留声明顺序与 Annotated）。

    ``__annotations__`` 只含当前类直接声明的字段，基类字段需要沿
    MRO 反向合并（基类在前，子类覆盖同名）。
    """
    merged: dict[str, Any] = {}
    for base in reversed(td.__mro__):
        if _is_typed_dict(base):
            merged.update(getattr(base, "__annotations__", {}))
    return merged


def _choices_schema(py_type: Any) -> dict[str, Any]:
    """把自定义类型（dataclass / TypedDict / NamedTuple）展开为对象 schema。

    - dataclass：字段 = dataclass.fields，无默认值 / 无 default_factory 为
      必填；字段类型经 ``get_type_hints`` 展开（``from __future__ import
      annotations`` 下注解是字符串，需要展开成实际类型再递归），
      ``Annotated`` 描述保留。
    - TypedDict：见 ``_typed_dict_schema``。
    - NamedTuple：字段 = ``_fields``，默认值取 ``_field_defaults``；
      无默认值为必填。
    """
    if _is_typed_dict(py_type):
        return _typed_dict_schema(py_type)

    if dataclasses.is_dataclass(py_type):
        # is_dataclass 会把类型收窄成 DataclassInstance 联合，需要还原为 type
        dc_cls: type = cast(type, py_type)
        dc_fields = dataclasses.fields(dc_cls)
        raw = {f.name: f.type for f in dc_fields}
        resolved = _resolve_field_annotations(dc_cls, raw)
        required_list = [
            f.name for f in dc_fields
            if f.default is dataclasses.MISSING
            and f.default_factory is dataclasses.MISSING
        ]
        return {
            "type": "object",
            "properties": {
                f.name: _parse_type_with_meta(resolved[f.name])
                for f in dc_fields
            },
            "required": required_list,
        }

    if isinstance(py_type, type) and issubclass(py_type, tuple) and hasattr(py_type, "_fields"):
        nt_fields: list[str] = list(py_type._fields)
        raw = {
            name: getattr(py_type, "__annotations__", {}).get(name, Any)
            for name in nt_fields
        }
        resolved = _resolve_field_annotations(py_type, raw)
        defaults = getattr(py_type, "_field_defaults", {})
        required_list = [name for name in nt_fields if name not in defaults]
        return {
            "type": "object",
            "properties": {
                name: _parse_type_with_meta(resolved[name])
                for name in nt_fields
            },
            "required": required_list,
        }

    raise ValueError(f"Unsupported type: {py_type}")


def _typed_dict_schema(td: Any) -> dict[str, Any]:
    """TypedDict → {type: object, properties, required}。

    - required 取 ``__required_keys__``（继承的子类会正确合并），
      按字段声明顺序排列；
    - 字段标注用 ``_typed_dict_annotations`` 合并继承链并保留 Annotated。
    """
    merged = _typed_dict_annotations(td)
    resolved = _resolve_field_annotations(td, merged)
    required_keys = getattr(td, "__required_keys__", None)
    required_list = [
        name for name in merged
        if required_keys is None or name in required_keys
    ]
    return {
        "type": "object",
        "properties": {
            name: _parse_type_with_meta(resolved[name])
            for name in merged
        },
        "required": required_list,
    }


def _is_typed_dict(py_type: Any) -> bool:
    return (
        isinstance(py_type, type)
        and issubclass(py_type, dict)
        and hasattr(py_type, "__required_keys__")
    )


def _object_schema(py_type: Any) -> dict[str, Any] | None:
    """若 ``py_type`` 是可展开为对象 schema 的自定义类型则返回 schema，否则 None。"""
    try:
        # TypedDict / dataclass / NamedTuple 统一交给 _choices_schema 展开
        if _is_typed_dict(py_type):
            return _choices_schema(py_type)
        # dataclass（装饰的类或实例）
        if dataclasses.is_dataclass(py_type):
            return _choices_schema(py_type)
        # NamedTuple（tuple 子类带 _fields）
        if isinstance(py_type, type) and issubclass(py_type, tuple) and hasattr(py_type, "_fields"):
            return _choices_schema(py_type)
    except (AttributeError, TypeError):
        pass
    return None


def _build_json_schema(py_type: Any) -> dict[str, Any]:
    """
    递归构建任意类型对应的 JSON Schema 片段。
    - 标量：{"type": "integer"} 等
    - list[T]：{"type": "array", "items": <T 的 schema>}
    - dict[K, V]：{"type": "object", "additionalProperties": <V 的 schema>}
    - 自定义类型（dataclass / TypedDict / NamedTuple）：展开为带
      properties / required 的对象 schema（嵌套的通用 list[dict]
      仍保持裸 object，不强制约束内部字段）
    - Union/Optional 出现在任意层级都会被递归解包
    """
    origin = get_origin(py_type)

    # Literal['a', 'b'] → {type: ..., enum: ['a', 'b']}
    if origin is typing.Literal:
        return _literal_schema(get_args(py_type))

    # 若出现 Union / Optional（含 | None），先解出非 None 部分再递归构建
    if origin in (Union, types.UnionType):
        args = get_args(py_type)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _build_json_schema(non_none[0])

    # 自定义类型（非泛型 origin）→ 对象 schema
    obj_schema = _object_schema(py_type)
    if obj_schema is not None:
        return obj_schema

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

    def __new__(cls, *_: Any, **__: Any) -> "ToolsRegistry":
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
