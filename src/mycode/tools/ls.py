"""ls 工具：列出目录内容（自实现 ls -laF 风格）。"""
import datetime
import os
import stat
from typing import Annotated

from mycode.tools_registry import ToolsRegistry
from mycode.tools._safe_path import safe_path
from mycode.tools._truncate import cap_lines


_TYPE_CHAR = {
    stat.S_IFDIR: "d",
    stat.S_IFREG: "-",
    stat.S_IFLNK: "l",
    stat.S_IFCHR: "c",
    stat.S_IFBLK: "b",
    stat.S_IFIFO: "p",
    stat.S_IFSOCK: "s",
}


def _file_type_char(mode: int) -> str:
    """文件类型字符（d/-/l/c/b/p/s）。"""
    return _TYPE_CHAR.get(stat.S_IFMT(mode), "?")


def _type_suffix(mode: int) -> str:
    """ls -F 类型后缀符号。

    - 目录 → ``/``
    - 可执行普通文件 → ``*``
    - 符号链接 → ``@``
    - FIFO → ``|``
    - 套接字 → ``=``
    """
    fmt = stat.S_IFMT(mode)
    if fmt == stat.S_IFDIR:
        return "/"
    if fmt == stat.S_IFLNK:
        return "@"
    if fmt == stat.S_IFIFO:
        return "|"
    if fmt == stat.S_IFSOCK:
        return "="
    if fmt == stat.S_IFREG:
        if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            return "*"
    return ""


def _perm_string(mode: int) -> str:
    """9 位权限（rwxrwxrwx，不含 setuid/setgid/sticky）。"""
    perm = ""
    for r, w, x in (
        (stat.S_IRUSR, stat.S_IWUSR, stat.S_IXUSR),
        (stat.S_IRGRP, stat.S_IWGRP, stat.S_IXGRP),
        (stat.S_IROTH, stat.S_IWOTH, stat.S_IXOTH),
    ):
        perm += "r" if mode & r else "-"
        perm += "w" if mode & w else "-"
        perm += "x" if mode & x else "-"
    return perm


def _isoformat_local(mtime: float) -> str:
    """本地时区 aware datetime 的 ISO-8601（带偏移后缀）。

    输出形如 ``"2024-01-15T10:30:45+08:00"`` 或 UTC 环境下的
    ``"2024-01-15T02:30:45+00:00"``。
    """
    dt = datetime.datetime.fromtimestamp(mtime).astimezone()
    return dt.isoformat(timespec="seconds")


def _format_entry(name: str, st: os.stat_result) -> str:
    mode = st.st_mode
    size = st.st_size
    mtime = st.st_mtime
    mode_str = _file_type_char(mode) + _perm_string(mode)  # 10 字符
    date_str = _isoformat_local(mtime)
    suffix = _type_suffix(mode)
    return f"{mode_str}  {size:>8d}  {date_str}  {name}{suffix}"


@ToolsRegistry.tool(
    description=(
        "列出目录内容（类似 ls -laF）。每行包含：权限、字节大小、"
        "ISO-8601 日期（带时区偏移）、文件名与类型后缀"
        "（目录 /、可执行 *、符号链接 @、FIFO |、套接字 =）。"
        "按文件名排序。limit 限制条目数；truncate 限制输出字节数（KiB），"
        "任一上限触发即追加截断标记。"
    )
)
def ls(
    dir_path: Annotated[str, "目标目录"] = ".",
    limit: Annotated[int, "最多输出的条目数"] = 500,
    truncate: Annotated[int, "输出字节数上限（KiB）"] = 50,
) -> str:
    try:
        abs_path = safe_path(dir_path).abs
    except ValueError as e:
        return str(e)
    if not os.path.isdir(abs_path):
        return f"Error: 不是目录: {dir_path}"

    items: list[tuple[str, os.stat_result]] = []
    try:
        with os.scandir(abs_path) as it:
            for entry in it:
                # 不跟随符号链接，与 `ls -la` 行为一致
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    # broken link 等情况，用 lstat 重试
                    try:
                        st = os.lstat(os.path.join(abs_path, entry.name))
                    except OSError:
                        continue
                items.append((entry.name, st))
    except OSError as e:
        return f"Error: 读取目录失败: {e}"

    # 按名称排序（与默认 ls 一致）
    items.sort(key=lambda x: x[0])

    lines = [_format_entry(name, st) for name, st in items]
    body, _ = cap_lines(lines, max_lines=limit, max_kib=truncate)
    return body


__all__ = ["ls"]
