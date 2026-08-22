#!/usr/bin/env python3
"""grep 工具：在文件中搜索模式。优先 rg，无 rg 用 grep。"""
import shutil
import subprocess
from typing import Annotated

from mycode.tools_registry import ToolsRegistry
from mycode.tools._safe_path import safe_path
from mycode.tools._truncate import cap_lines


@ToolsRegistry.tool(
    description=(
        "在文件或目录中按模式搜索文本。优先使用 ripgrep (rg)，无 rg 时"
        "回退到 grep。返回带文件名与行号的匹配行。无匹配时返回 '(无匹配)'。"
        "limit 限制匹配行数；truncate 限制输出字节数（KiB），"
        "任一上限触发即追加截断标记。"
        "注意：fallback 到 grep 时不支持 .gitignore 过滤，必须显式设 no_ignore=true。"
    )
)
def grep(
    pattern: Annotated[str, "要搜索的模式（正则或字面量）"],
    path: Annotated[str, "搜索路径（文件或目录）"] = ".",
    glob: Annotated[str | None, "文件名 glob 过滤（如 '*.py'）"] = None,
    ignore_case: Annotated[bool, "忽略大小写"] = False,
    literal: Annotated[bool, "按字面量匹配（关闭正则元字符）"] = False,
    context: Annotated[int, "匹配前后输出几行上下文"] = 0,
    no_ignore: Annotated[bool, "不忽略 .gitignore（fallback 到 grep 时必须为 true）"] = False,
    limit: Annotated[int, "最多返回的匹配行数"] = 100,
    truncate: Annotated[int, "输出字节数上限（KiB）"] = 50,
) -> str:
    try:
        sp = safe_path(path)
    except ValueError as e:
        return str(e)

    proc: subprocess.CompletedProcess[str]
    if shutil.which("rg"):
        cmd: list[str] = ["rg", "--no-heading", "--line-number"]
        if ignore_case:
            cmd.append("-i")
        if literal:
            cmd.append("-F")
        if no_ignore:
            cmd.append("--no-ignore")
        if context and context > 0:
            cmd.extend(["-C", str(context)])
        if glob:
            cmd.extend(["--glob", glob])
        cmd.extend(["--", pattern, sp.rel])
        # cwd=wksp：相对路径入口 + 相对工作区的输出，避免长前缀
        proc = subprocess.run(
            cmd, cwd=sp.wksp, capture_output=True, text=True,
        )
        # rg 无匹配时退出码 1，但 stdout 通常为空且无错误
        if proc.returncode not in (0, 1):
            return f"Error: rg 执行失败\n{proc.stderr}"
    elif shutil.which("grep"):
        # grep 原生不支持 .gitignore；no_ignore=False（默认）会期望过滤，
        # 此处直接拒绝，避免"以为得到了 .gitignore 过滤实际没有"。
        if not no_ignore:
            return (
                "Error: fallback 到 grep 不支持 .gitignore，"
                "请安装 ripgrep (rg) 后再试；"
                "或显式设置 no_ignore=true 接受此限制"
            )
        cmd = ["grep", "-rn", "-H", "--"]
        if ignore_case:
            cmd.append("-i")
        if literal:
            cmd.append("-F")
        if context and context > 0:
            cmd.extend(["-C", str(context)])
        if glob:
            cmd.extend(["--include", glob])
        cmd.extend([pattern, sp.rel])
        proc = subprocess.run(
            cmd, cwd=sp.wksp, capture_output=True, text=True,
        )
        # grep 无匹配退出码 1；2 表示真正错误
        if proc.returncode not in (0, 1):
            return f"Error: grep 执行失败\n{proc.stderr}"
    else:
        return "Error: 系统既未找到 rg 也未找到 grep"

    output = proc.stdout.rstrip("\n")
    if not output:
        return "(无匹配)"

    lines = output.splitlines()
    body, _ = cap_lines(lines, max_lines=limit, max_kib=truncate)
    return body


__all__ = ["grep"]
