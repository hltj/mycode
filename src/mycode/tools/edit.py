#!/usr/bin/env python3
"""edit 工具：在文件中替换文本。"""

import os
from typing import Annotated

from mycode.tools_registry import ToolsRegistry
from mycode.tools._safe_path import safe_path


@ToolsRegistry.tool(
    description=(
        "在文件中按 old_text/new_text 替换内容。"
        "默认替换首次出现；replace_all=true 时替换全部。"
        "old_text 找不到时返回错误；old_text 不唯一且未开启 replace_all 时返回错误。"
    )
)
def edit(
    file_path: Annotated[str, "目标文件路径"],
    old_text: Annotated[str, "要被替换的原文本"],
    new_text: Annotated[str, "替换后的文本"],
    replace_all: Annotated[bool, "是否替换所有匹配"] = False,
) -> str:
    try:
        abs_path = safe_path(file_path)
    except ValueError as e:
        return str(e)

    if not os.path.isfile(abs_path):
        return f"Error: 不是文件或不存在: {file_path}"

    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return f"Error: 读取失败: {e}"

    count = content.count(old_text)
    if count == 0:
        return f"Error: 在文件 {file_path} 中未找到 old_text"
    if count > 1 and not replace_all:
        return f"Error: old_text 不唯一，存在 {count} 处匹配，无法替换"

    if replace_all:
        new_content = content.replace(old_text, new_text)
        actual = count
    else:
        new_content = content.replace(old_text, new_text, 1)
        actual = 1

    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as e:
        return f"Error: 写入失败: {e}"

    return f"已替换 {actual} 处"


__all__ = ["edit"]
