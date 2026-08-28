"""glob 工具：按模式匹配文件路径，优先 fd，无 fd 用 find。"""
import shutil
import subprocess
from typing import Annotated

from mycode.tools_registry import ToolsRegistry
from mycode.tools._safe_path import safe_path
from mycode.tools._truncate import cap_lines


@ToolsRegistry.tool(
    description=(
        "按 glob 模式匹配文件路径（如 '*.py'、'**/*.md'）。"
        "优先使用 fd，无 fd 时回退到 find。无匹配时返回 '(无匹配)'。"
        "limit 限制匹配数；truncate 限制输出字节数（KiB），"
        "任一上限触发即追加截断标记。"
    )
)
def glob(
    dir_path: Annotated[str, "搜索起始目录"] = ".",
    pattern: Annotated[str, "glob 匹配模式（如 *.py、**/*.md）"] = "*",
    limit: Annotated[int, "最多返回的匹配路径数"] = 1000,
    truncate: Annotated[int, "输出字节数上限（KiB）"] = 50,
) -> str:
    try:
        sp = safe_path(dir_path)
    except ValueError as e:
        return str(e)

    proc: subprocess.CompletedProcess[str]
    if shutil.which("fd"):
        # fd 默认 regex 模式，--glob 切到 glob（通配符）模式；
        # 用法：fd [选项] <pattern> <rel> -C <wksp>
        # -C/--base-directory 切到 wksp，让输出相对工作区、前缀更短。
        cmd = [
            "fd", "--glob", "--type", "f", "--type", "d",
            pattern, sp.rel, "-C", sp.wksp,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            return f"Error: fd 执行失败\n{proc.stderr}"
    elif shutil.which("find"):
        # find 的 -name 本身就按 glob 模式；以 wksp 为 cwd 使输出相对工作区。
        cmd = ["find", sp.rel, "-name", pattern]
        proc = subprocess.run(
            cmd, cwd=sp.wksp, capture_output=True, text=True,
        )
        if proc.returncode not in (0, 1):
            return f"Error: find 执行失败\n{proc.stderr}"
    else:
        return "Error: 系统既未找到 fd 也未找到 find"

    matches = [m for m in proc.stdout.splitlines() if m]
    if not matches:
        return "(无匹配)"

    body, _ = cap_lines(matches, max_lines=limit, max_kib=truncate)
    return body


__all__ = ["glob"]
