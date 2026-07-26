#!/usr/bin/env python3
"""read 工具：读取文件内容（cat -n 风格，带行号）。"""
import os
from typing import Annotated

from mycode.tools_registry import ToolsRegistry
from mycode.tools._safe_path import safe_path
from mycode.tools._truncate import cap_lines


@ToolsRegistry.tool(
    description=(
        "读取文件内容并以 cat -n 风格输出（每行带行号）。"
        "offset 是起始行号（从 1 开始），limit 是最多读取的行数，"
        "truncate 是输出总字节数上限（KiB），"
        "limit 与 truncate 任一触发即停止并追加截断标记。"
    )
)
def read(
    file_path: Annotated[str, "要读取的文件路径"],
    offset: Annotated[int, "起始行号（从 1 开始）"] = 1,
    limit: Annotated[int | None, "最多读取的行数；None 表示不限"] = None,
    truncate: Annotated[int, "输出字节数上限（KiB）"] = 50,
) -> str:
    try:
        abs_path = safe_path(file_path)
    except ValueError as e:
        return str(e)

    if not os.path.isfile(abs_path):
        return f"Error: 不是文件或不存在: {file_path}"

    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as e:
        return f"Error: 读取失败: {e}"

    if offset < 1:
        offset = 1
    start_idx = offset - 1
    if start_idx >= len(all_lines):
        return f"(文件仅 {len(all_lines)} 行，offset 越界)"

    end_idx = len(all_lines)
    if limit is not None and limit > 0:
        end_idx = min(end_idx, start_idx + limit)

    selected = all_lines[start_idx:end_idx]
    width = len(str(end_idx))
    numbered = [
        f"{i:>{width}d}\t{line.rstrip(chr(10))}"
        for i, line in enumerate(selected, start=offset)
    ]

    body, truncated = cap_lines(numbered, max_lines=limit, max_kib=truncate)

    # 仅当未触截断、且 offset/limit 后仍有剩余时，附剩余提示
    extra = ""
    if not truncated and end_idx < len(all_lines):
        extra = f"\n... 剩余 {len(all_lines) - end_idx} 行未显示（已设置 offset/limit）"

    return body + extra


__all__ = ["read"]
