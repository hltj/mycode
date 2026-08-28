"""write 工具：写入文件（覆盖式）。"""

import os
from typing import Annotated

from mycode.tools_registry import ToolsRegistry
from mycode.tools._safe_path import safe_path


@ToolsRegistry.tool(
    description=(
        "将 content 写入 file_path（覆盖已有内容）；父目录不存在会自动创建。"
    )
)
def write(
    file_path: Annotated[str, "目标文件路径"],
    content: Annotated[str, "要写入的完整内容"],
) -> str:
    try:
        abs_path = safe_path(file_path).abs
    except ValueError as e:
        return str(e)

    parent = os.path.dirname(abs_path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return f"Error: 创建父目录失败: {e}"

    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            n = f.write(content)
    except OSError as e:
        return f"Error: 写入失败: {e}"

    return f"已写入 {n} 字节到 {abs_path}"


__all__ = ["write"]
