"""
内置工具集合。

导入此子包会自动将所有工具注册到 ``mycode.tools_registry.ToolsRegistry``。
同时将各工具函数作为本包属性暴露，便于 ``mycode.tools.bash`` 形式调用。
"""

# 导入各工具函数：先导入子模块触发装饰器注册，再从子模块取函数提升到包级
from mycode.tools import bash as _bash_mod  # noqa: F401, E402
from mycode.tools.bash import bash as bash  # noqa: E402

from mycode.tools import edit as _edit_mod  # noqa: F401, E402
from mycode.tools.edit import edit as edit  # noqa: E402

from mycode.tools import glob as _glob_mod  # noqa: F401, E402
from mycode.tools.glob import glob as glob  # noqa: E402

from mycode.tools import grep as _grep_mod  # noqa: F401, E402
from mycode.tools.grep import grep as grep  # noqa: E402

from mycode.tools import ls as _ls_mod  # noqa: F401, E402
from mycode.tools.ls import ls as ls  # noqa: E402

from mycode.tools import patch as _patch_mod  # noqa: F401, E402
from mycode.tools.patch import patch as patch  # noqa: E402

from mycode.tools import read as _read_mod  # noqa: F401, E402
from mycode.tools.read import read as read  # noqa: E402

from mycode.tools import todo_write as _todo_write_mod  # noqa: F401, E402
from mycode.tools.todo_write import todo_write as todo_write  # noqa: E402

from mycode.tools import write as _write_mod  # noqa: F401, E402
from mycode.tools.write import write as write  # noqa: E402

__all__ = [
    "bash",
    "edit",
    "glob",
    "grep",
    "ls",
    "patch",
    "read",
    "todo_write",
    "write",
]
