#!/usr/bin/env python3
"""
TDD tests for tools.py
"""
import importlib
import os

import mycode.tools as tools
from mycode.tools_registry import ToolsRegistry


def _ensure_registered():
    """确保工具已注册（模块导入时已注册，但 reset 后会清掉）

    包结构下需要 reload 子模块 ``mycode.tools.bash``，因为 reload 包本身
    不会重新执行子模块文件，装饰器不会再次触发。
    使用 ``importlib.import_module`` 拿真正的子模块（包 ``__init__`` 里
    ``from mycode.tools.bash import bash`` 会把 ``mycode.tools.bash`` 属性
    覆盖为函数，直接 import 拿不到子模块对象）。
    """
    if "bash" not in [t["function"]["name"] for t in ToolsRegistry.get_tools()]:
        import importlib as _il
        bash_mod = _il.import_module("mycode.tools.bash")
        _il.reload(bash_mod)
        # 重新触发 ``mycode.tools`` 包初始化以恢复 ``bash`` 函数属性
        _il.reload(tools)


def test_bash_tool_registered():
    """测试 bash 工具已正确注册"""
    _ensure_registered()
    tool_def = ToolsRegistry.get_tool_def("bash")
    assert tool_def is not None
    assert tool_def["function"]["name"] == "bash"
    assert tool_def["function"]["description"] == "运行 bash 命令"
    assert tool_def["type"] == "function"
    params = tool_def["function"]["parameters"]
    assert params["properties"]["command"]["type"] == "string"
    assert "command" in params["required"]


def test_bash_tool_handler_exists():
    """测试 bash 工具处理函数存在"""
    _ensure_registered()
    handler = ToolsRegistry.get_handler("bash")
    assert handler is not None
    assert callable(handler)


def test_bash_execute_simple_command():
    """测试 bash 执行简单命令"""
    result = tools.bash("echo hello")
    assert "hello" in result


def test_bash_execute_pwd():
    """测试 bash 执行 pwd 命令"""
    result = tools.bash("pwd")
    assert os.getcwd() in result


def test_bash_timeout():
    """测试 bash 超时处理"""
    saved = os.environ.get('BASH_TIMEOUT')
    os.environ['BASH_TIMEOUT'] = '1'
    result = tools.bash("sleep 10")
    assert "Timeout" in result
    if saved is not None:
        os.environ['BASH_TIMEOUT'] = saved
    else:
        os.environ.pop('BASH_TIMEOUT', None)


def test_bash_dangerous_command_blocked():
    """测试危险命令被阻止"""
    saved = os.environ.get('BASH_DANGEROUS')
    os.environ['BASH_DANGEROUS'] = 'echo_dangerous_test_12345'
    result = tools.bash("echo_dangerous_test_12345")
    assert "Dangerous command blocked" in result
    if saved is not None:
        os.environ['BASH_DANGEROUS'] = saved
    else:
        os.environ.pop('BASH_DANGEROUS', None)


def test_tools_includes_bash():
    """测试工具列表中包含 bash"""
    _ensure_registered()
    sdk_tools = ToolsRegistry.get_tools()
    tool_names = [t["function"]["name"] for t in sdk_tools]
    assert "bash" in tool_names


def test_bash_tool_description():
    """测试 bash 工具的 command 参数描述"""
    _ensure_registered()
    tool_def = ToolsRegistry.get_tool_def("bash")
    params = tool_def["function"]["parameters"]
    assert params["properties"]["command"]["description"] == "要执行的 bash 命令"
