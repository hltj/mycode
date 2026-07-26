#!/usr/bin/env python3
"""
输出截断工具。

两个工具：

- :func:`cap_lines` —— 行数 + KiB 联合截断（字节流驱动、逐行扫描）。
  任何一行要么完整保留、要么完全丢弃，绝不在行内切断。
  ls / glob / grep 都使用此工具。

"""
from __future__ import annotations

from typing import Iterable

DEFAULT_MARKER = "\n... 已截断"


def cap_lines(
    lines: Iterable[str],
    *,
    max_lines: int | None = None,
    max_kib: int | None = None,
    marker: str = DEFAULT_MARKER,
) -> tuple[str, bool]:
    """逐行扫描并在任意一条红线触发时立即停止。

    :param lines: 不含末尾换行符的行序列。可为任意可迭代对象。
    :param max_lines: 最多保留的行数（None 表示不限）。
    :param max_kib: 最多输出的字节数（含 marker），1 KiB = 1024 字节。
    :param marker: 截断触发时附加在末尾的标记字符串。
    :returns: ``(joined_text, truncated)``。

    红线顺序：每读取一行时，先检查"加入该行后总字节是否超过 KiB 上限"，
    再检查"行数是否超过 max_lines"。任一触发立即停止消费并追加 marker。

    行间分隔符为单个 ``\\n``，计入字节预算。任意行均按 utf-8 字节
    完整判断长度——绝不截断行内字符。
    """
    if max_lines is None and max_kib is None:
        body = "\n".join(lines)
        return body, False

    # 为 marker 预留字节，保证 body + marker 不超 max_kib
    marker_bytes = len(marker.encode("utf-8"))
    byte_budget: int | None = None
    if max_kib is not None and max_kib > 0:
        byte_budget = max(0, max_kib * 1024 - marker_bytes)

    used_bytes = 0
    count = 0
    pieces: list[str] = []
    truncated = False

    for line in lines:
        line_byte_len = len(line.encode("utf-8"))
        need_sep = count > 0
        add_bytes = line_byte_len + (1 if need_sep else 0)

        # 红线 1：字节预算
        if byte_budget is not None and used_bytes + add_bytes > byte_budget:
            truncated = True
            # 若当前是首行且本身已超预算（不可能完整放入），
            # 完整保留该行并立即停止，避免"明明有内容却什么都不显示"
            if count == 0 and need_sep is False and line_byte_len > byte_budget:
                pieces.append(line)
                used_bytes += line_byte_len
                count += 1
            break

        # 红线 2：行数预算
        if max_lines is not None and count >= max_lines:
            truncated = True
            break

        if need_sep:
            pieces.append("\n")
            used_bytes += 1
        pieces.append(line)
        used_bytes += line_byte_len
        count += 1

    body = "".join(pieces)
    if truncated:
        body += marker
    return body, truncated


__all__ = ["cap_lines", "DEFAULT_MARKER"]
