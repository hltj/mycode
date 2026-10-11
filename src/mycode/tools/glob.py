"""glob 工具：按模式匹配文件路径，优先 fd，其次 rg，都没有才用 find。"""
import shutil
import subprocess
from typing import Annotated

from mycode.tools_registry import ToolsRegistry
from mycode.tools._safe_path import safe_path
from mycode.tools._truncate import cap_lines


@ToolsRegistry.tool(
    description=(
        "按 glob 模式匹配文件路径，如 '*.py'。"
        "注意：glob 只按文件名（basename）匹配，不支持目录路径："
        "模式中不允许出现 '/'（如 'foo/bar/*.py' 匹配不到任何文件）。"
        "限定目录请用 dir_path 参数。"
        "优先使用 fd，其次 rg，最后回退到 find。无匹配时返回 '(无匹配)'。"
        "limit 限制匹配数；truncate 限制输出字节数（KiB），"
        "任一上限触发即追加截断标记。"
        "注意：fallback 到 find 时不支持 .gitignore 过滤，必须显式设 no_ignore=true。"
    )
)
def glob(
    dir_path: Annotated[str, "搜索起始目录"] = ".",
    pattern: Annotated[str, "glob 匹配模式（如 *.py）"] = "*",
    no_ignore: Annotated[bool, "不忽略 .gitignore（fallback 到 find 时必须为 true）"] = False,
    include_hidden: Annotated[bool, "包含隐藏文件（点开头的文件或目录）"] = False,
    limit: Annotated[int, "最多返回的匹配路径数"] = 1000,
    truncate: Annotated[int, "输出字节数上限（KiB）"] = 50,
) -> str:
    try:
        sp = safe_path(dir_path)
    except ValueError as e:
        return str(e)

    # fd/find 的 glob 只匹配文件名本身（basename），模式中的 '/' 是
    # 路径分隔符。fd 只对含 '/' 且无通配符的模式报错（如 'foo/bar'）；
    # 一旦带上通配符（如 'foo/bar/*.py'）就静默零匹配、无任何提示。
    # find 的 -name 含 '/' 时该谓词对任何文件求值恒为假，仅打印警告。
    # 因此含 '/' 的写法一律在此明确报错，避免“查不到但不报错”的困惑。
    if not pattern or "/" in pattern:
        return (
            "Error: glob 模式不支持路径分隔符 '/'（glob 只按文件名匹配）："
            f"模式 '{pattern}' 匹配不到任何文件。"
            "请把目录放进 dir_path 参数、模式只写文件名，例如 "
            "glob(dir_path='src/mycode', pattern='*.py')。"
        )

    proc: subprocess.CompletedProcess[str]
    if shutil.which("fd"):
        # fd 默认 regex 模式，--glob 切到 glob（通配符）模式；
        # 用法：fd [选项] <pattern> <rel> -C <wksp>
        # -C/--base-directory 切到 wksp，让输出相对工作区、前缀更短。
        # fd 默认不列隐藏文件且遵循 .gitignore，--hidden / --no-ignore 按需放开。
        cmd = ["fd", "--glob", "--type", "f", "--type", "d"]
        if no_ignore:
            cmd.append("--no-ignore")
        if include_hidden:
            cmd.append("--hidden")
        cmd.extend([pattern, sp.rel, "-C", sp.wksp])
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            return f"Error: fd 执行失败\n{proc.stderr}"
    elif shutil.which("rg"):
        # rg --files 只列文件（不含目录）；--glob 切到 glob（通配符）模式。
        # rg 与 fd 的默认行为有差异：指定 include glob（如 --glob '*.py'）时，
        # 匹配 glob 的隐藏文件也会被列出。include_hidden=False 时末尾补
        # --glob '!.*'（rg 规则以后出现的 glob 为准），与 fd 的“默认不列
        # 隐藏文件/目录”保持一致。
        cmd = ["rg", "--files"]
        if no_ignore:
            cmd.append("--no-ignore")
        if include_hidden:
            cmd.append("--hidden")
        cmd.extend(["--glob", pattern])
        if not include_hidden:
            cmd.extend(["--glob", "!.*"])
        cmd.append(sp.rel)
        # cwd=wksp：相对路径入口 + 相对工作区的输出，避免长前缀
        proc = subprocess.run(
            cmd, cwd=sp.wksp, capture_output=True, text=True,
        )
        # rg 无匹配时退出码 1
        if proc.returncode not in (0, 1):
            return f"Error: rg 执行失败\n{proc.stderr}"
    elif shutil.which("find"):
        # find 原生不支持 .gitignore；no_ignore=False（默认）会期望过滤，
        # 此处直接拒绝，避免“以为得到了 .gitignore 过滤实际没有”。
        if not no_ignore:
            return (
                "Error: fallback 到 find 不支持 .gitignore，"
                "请安装 fd 或 ripgrep (rg) 后再试；"
                "或显式设置 no_ignore=true 接受此限制"
            )
        # find 的 -name 本身就按 glob 模式；以 wksp 为 cwd 使输出相对工作区。
        # find 默认不区分隐藏文件；include_hidden=False 时用排除模式
        # -not -path '*/.*' 剔除路径中含隐藏组件的条目（根 '.' 本身不受影响）。
        cmd = ["find", sp.rel]
        if not include_hidden:
            cmd.extend(["-not", "-path", "*/.*"])
        cmd.extend(["-name", pattern])
        proc = subprocess.run(
            cmd, cwd=sp.wksp, capture_output=True, text=True,
        )
        if proc.returncode not in (0, 1):
            return f"Error: find 执行失败\n{proc.stderr}"
    else:
        return "Error: 系统既未找到 fd、rg，也未找到 find"

    matches = [m for m in proc.stdout.splitlines() if m]
    if not matches:
        return "(无匹配)"

    body, _ = cap_lines(matches, max_lines=limit, max_kib=truncate)
    return body


__all__ = ["glob"]
