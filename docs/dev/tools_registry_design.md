# 工具注册系统设计

## 概述

mycode 通过 `ToolsRegistry`（`src/mycode/tools_registry.py`）把工具定义与执行统一管理起来：以装饰器方式注册工具函数，自动从 Python 函数签名提取 JSON Schema 参数定义，生成 OpenAI SDK 原生类型 `ChatCompletionFunctionToolParam`，供 `client.chat.completions.create(tools=...)` 直接使用。运行时再按工具名从注册表取回 handler 执行。

核心文件：
- `src/mycode/tools_registry.py` — 注册表、JSON Schema 推导、装饰器
- `src/mycode/tools/` — 内置工具实现（导入即注册）

---

## 一、设计目标与约束

1. **零样板**：工具作者只需写普通函数 + 类型注解，装饰器自动产出 SDK 工具定义。
2. **类型安全**：直接返回 OpenAI SDK 原生类型（`ChatCompletionFunctionToolParam`），不引入自定义包装层，保证与 SDK / mypy 兼容。
3. **参数自描述**：参数描述通过 `typing.Annotated[Type, "描述"]` 提供，可选的 `description` / `name` 装饰器参数覆盖函数 docstring / 函数名。
4. **禁止实例化**：注册表是纯类方法容器，运行时 `ToolsRegistry()` 直接抛 `TypeError`。

---

## 二、ToolsRegistry（类方法容器）

```python
class ToolsRegistry:
    _tools: list[ChatCompletionFunctionToolParam] = []  # 工具定义（注册顺序）
    _handlers: dict[str, Callable] = {}                 # 工具名 → 处理函数

    def __new__(cls, *_, **__) -> NoReturn:  # 禁止实例化

    @classmethod
    def reset(cls) -> None: ...             # 清空注册表（测试用）
    @classmethod
    def tool(cls, _func=None, *, description=None, name=None) -> ...: ...  # 装饰器
    @classmethod
    def get_tools(cls) -> list[ChatCompletionFunctionToolParam]: ...
    @classmethod
    def get_handler(cls, tool_name: str) -> Callable | None: ...   # O(1) 查找
    @classmethod
    def get_tool_def(cls, tool_name: str) -> ChatCompletionFunctionToolParam | None: ...
```

### 装饰器两种用法

```python
@ToolsRegistry.tool                        # 无参：description 取 docstring，name 取函数名
def my_func(x: int) -> str: ...

@ToolsRegistry.tool(description="...", name="custom")   # 有参：覆盖 docstring / 函数名
def my_func(x: int) -> str: ...
```

装饰器内部流程：
1. 取 `name or f.__name__` 作为工具名，`description or inspect.getdoc(f) or ""` 作为描述；
2. `_extract_parameters(f)` 从签名生成 `parameters` JSON Schema；
3. 构造 `ChatCompletionFunctionToolParam(type="function", function=FunctionDefinition(...))`；
4. 存入 `_tools`（追加到尾部，保持注册顺序）与 `_handlers[t_name] = f`；
5. 原样返回函数 `f`（可继续被直接调用）。

### 注册顺序

`_tools` 是追加式列表，顺序即导入顺序。`mycode/tools/__init__.py` 按固定顺序导入各工具模块触发装饰器注册，因此 `get_tools()` 返回顺序稳定。`agent_loop` 每次调用模型时都取 `get_tools()` 传给 API。

---

## 三、JSON Schema 推导

### 3.1 类型映射（_type_to_json_schema_type）

| Python 类型 | JSON Schema type |
|-------------|------------------|
| `int`     | `integer` |
| `float`   | `number`  |
| `str`     | `string`  |
| `bool`    | `boolean` |
| `list`    | `array`   |
| `dict`    | `object`  |

不支持的标量类型（如 `bytes`、自定义类）抛 `ValueError("Unsupported type: ...")`。

### 3.2 泛型递归（_build_json_schema）

递归构建任意类型对应的 JSON Schema 片段：

- 标量：`{"type": "integer"}` 等
- `list[T]`：`{"type": "array", "items": <T 的 schema>}`
- `dict[K, V]`：`{"type": "object", "additionalProperties": <V 的 schema>}`
- `Union` / `Optional`（含 `T | None`）出现在**任意层级**都会被递归解包：
  先取 `get_args`，过滤掉 `NoneType`，若只剩一个类型则递归构建该类型
  （`Optional[x]` → `x` 的 schema；`list[str | None]` → `items` 为 `string`）。

`list` / `dict` 同时兼容内置泛型（`list[T]`、`dict[K,V]`）与 `typing` 旧语法
（`List[T]`、`Dict[K,V]`），通过 `_LIST_ORIGINS` / `_DICT_ORIGINS` 元组判断 origin。

### 3.3 参数解析（_parse_param）

`_parse_param(inspect.Parameter)` 处理单个参数：

1. 无类型注解 → 抛 `TypeError("参数 \"{name}\" 缺少类型注解")`；
2. 若注解是 `typing.Annotated[T, "描述", ...]`：取 `T` 为基类型，把所有字符串参数
   用空格拼接为 `description`；
3. `_build_json_schema(base_type)` 产出类型片段，有描述时附加
   `{"description": ...}`。

### 3.4 签名提取（_extract_parameters）

```python
sig = inspect.signature(func)
# 跳过 *args / **kwargs（VAR_POSITIONAL / VAR_KEYWORD）
properties = {name: _parse_param(param) for name, param in valid_params}
required   = [name for name, param in valid_params if param.default == inspect.Parameter.empty]

{"type": "object", "properties": properties, "required": required}
```

- 无默认值的参数进 `required`，有默认值的参数不进；
- `*args` / `**kwargs` 既不出现在 `properties` 也不出现在 `required`。

---

## 四、运行时查找

```python
handler = ToolsRegistry.get_handler(func_name)   # 执行
tool_def = ToolsRegistry.get_tool_def(func_name) # 定义（测试/校验）
```

- `get_handler`：`dict` O(1) 查找，未注册返回 `None`。
  `agent_loop` 中 `handler is None` 时返回 `"Error: 未知工具 '{func_name}'"`。
- `get_tool_def`：线性扫描 `_tools` 按 `function.name` 匹配，返回首个命中或 `None`。

---

## 五、内置工具注册

`src/mycode/tools/__init__.py` 导入各工具子模块触发装饰器注册，并把函数提升到包级暴露：

| 模块 | 工具名 | 类别（mode.py） | 说明 |
|------|--------|-----------------|------|
| `bash.py` | `bash` | dangerous / caution / unknown | 执行 shell 命令 |
| `write.py` | `write` | write | 覆盖写入文件 |
| `edit.py` | `edit` | write | old_text/new_text 替换 |
| `patch.py` | `patch` | write | 应用 unified diff |
| `ls.py` | `ls` | read | 列出目录内容 |
| `glob.py` | `glob` | read | 按 glob 匹配路径 |
| `grep.py` | `grep` | read | 搜索文本 |
| `read.py` | `read` | read | 带行号读文件 |
| `todo_write.py` | `todo_write` | internal | 内存待办列表 |

分类由 `mode.classify_tool` 依据工具名查表完成，与注册表解耦。

---

## 六、开发规范

1. **新增工具**：在 `src/mycode/tools/` 新建模块，用 `@ToolsRegistry.tool(...)`
   装饰工具函数；在 `tools/__init__.py` 导入该模块以触发注册。
2. **参数约束**：所有参数必须带类型注解；需要描述用
   `Annotated[Type, "描述"]`；文件类工具建议复用 `safe_path()`，大输出用 `cap_lines()`。
3. **保持注册顺序稳定**：`__init__.py` 的导入顺序即 `get_tools()` 顺序。
4. **禁止直接 new 注册表**：使用类方法，`ToolsRegistry()` 是错误用法。
5. **测试**：见 `tests/test_tools_registry.py`（类型映射、泛型递归、新旧语法
   混搭、Optional 深层解包、装饰器等价性等）。
