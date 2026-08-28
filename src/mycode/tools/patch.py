"""patch 工具：在目录中应用 unified diff。"""
import os
import re
import shutil
import subprocess
import tempfile
from typing import Annotated

from mycode.tools_registry import ToolsRegistry
from mycode.tools._safe_path import safe_path


def _detect_p(diff: str) -> str:
    """根据 diff 头部推测合适的 -p 级别。

    - ``--- a/foo`` / ``--- b/foo``（git diff 风格）→ "1"
    - 否则视为单层路径 → "0"
    """
    m = re.search(r"^---\s+(\S+)", diff, re.MULTILINE)
    if not m:
        return "0"
    path = m.group(1)
    if path.startswith("a/") or path.startswith("b/"):
        return "1"
    return "0"


@ToolsRegistry.tool(
    description=(
        "在目录中应用 unified diff（git/patch 风格）。"
        "diff 头部若为 ``--- a/path`` / ``--- b/path`` 自动使用 -p1，"
        "否则 -p0。先 dry-run 校验再应用，失败不会破坏现场。"
    )
)
def patch(
    diff: Annotated[str, "unified diff 文本"],
    dir_path: Annotated[str, "目标目录路径（相对或绝对，相对当前目录）"] = ".",
) -> str:
    try:
        abs_dir = safe_path(dir_path).abs
    except ValueError as e:
        return str(e)

    if shutil.which("patch") is None:
        return "Error: 系统未找到 patch 命令"

    p_level = _detect_p(diff)

    # 将 diff 写入临时文件
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".diff", delete=False, encoding="utf-8"
    )
    try:
        tmp.write(diff)
        tmp.flush()
        tmp_path = tmp.name
    finally:
        tmp.close()

    try:
        # 先 dry-run，避免破坏现场
        dry = subprocess.run(
            ["patch", f"-p{p_level}", "--dry-run", "-d", abs_dir, "-i", tmp_path],
            capture_output=True, text=True,
        )
        if dry.returncode != 0:
            return (
                f"Error: dry-run 失败（未应用，p={p_level}）\n"
                f"{dry.stderr or dry.stdout}"
            )

        real = subprocess.run(
            ["patch", f"-p{p_level}", "-d", abs_dir, "-i", tmp_path],
            capture_output=True, text=True,
        )
        if real.returncode != 0:
            return f"Error: 应用补丁失败\n{real.stderr or real.stdout}"

        return f"已应用补丁（-p{p_level}）到 {abs_dir}\n{(real.stdout or '').rstrip()}"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


__all__ = ["patch"]
