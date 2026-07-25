"""
内置工具集合。

导入此子包会自动将所有工具注册到 ``mycode.tools_registry.ToolsRegistry``。
同时将各工具函数作为本包属性暴露，便于 ``mycode.tools.bash`` 形式调用。
"""

from __future__ import annotations

# 导入各工具模块以触发其装饰器注册逻辑；
# 并把工具函数提升为包级属性，覆盖子模块属性的查找。
from mycode.tools.bash import bash  # noqa: F401, E402

__all__ = ["bash"]
