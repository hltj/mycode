#!/usr/bin/env python3
"""
路径安全检查模块。

任何接收 ``path`` 参数的工具，都应在处理前调用 :func:`safe_path`
获得规范化后的路径信息。CWD 之外的路径、或命中
``MYCODE_PROTECTED_PATH_PATTERN`` 环境变量所声明正则的路径，
均被视为不安全，调用方应返回 ``"Error: ..."`` 字符串给模型。

``safe_path`` 返回一个 :class:`SafePath`，同时携带三项：

- ``wksp``：工作区（CWD）绝对路径；
- ``abs``：请求路径的绝对路径；
- ``rel``：请求路径相对工作区的规范化路径（等于工作区时为 ``"."``）。

工具约定：内置的 ``read`` / ``write`` / ``edit`` / ``ls`` / ``patch``
与渲染层用 ``.abs`` 做实际读写；``glob`` / ``grep`` 把 ``.wksp`` +
``.rel`` 交给子进程，让输出路径前缀相对工作区、尽量简短。

环境变量格式：
    ``MYCODE_PROTECTED_PATH_PATTERN``，逗号分隔的多条正则表达式。
    任一正则命中绝对路径即视为不安全。

软链接会通过 ``os.path.realpath`` 跟随到真实路径后再做安全判断。
"""

import os
import re
from dataclasses import dataclass

PROTECTED_ENV = "MYCODE_PROTECTED_PATH_PATTERN"


def _load_patterns() -> list[str]:
    """读取并切分受保护路径正则列表。"""
    raw = os.getenv(PROTECTED_ENV, "")
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass(frozen=True)
class SafePath:
    """``safe_path`` 的结果。

    - ``wksp``：工作区绝对路径；
    - ``abs``：请求路径的绝对路径；
    - ``rel``：请求路径相对工作区的规范化路径，等于工作区时为 ``"."``。
    """

    wksp: str
    abs: str
    rel: str


def safe_path(path: str) -> SafePath:
    """规范化路径并验证其安全性。

    :raises ValueError: 路径不安全（越界 CWD 或命中保护正则），
        错误信息以 ``"Error: "`` 开头，便于工具函数直接透传。
    :returns: :class:`SafePath`，含 ``wksp`` / ``abs`` / ``rel`` 三项。
    """
    if not path:
        raise ValueError("Error: 路径不能为空")

    # realpath 同时完成绝对化 + 解析 .. + 跟随软链接
    abs_path = os.path.realpath(os.path.abspath(path))
    wksp = os.path.realpath(os.getcwd())

    # 在 CWD 下判定：相对路径若以 ".." 开头即视为越界
    try:
        rel = os.path.relpath(abs_path, wksp)
    except ValueError:
        # 不同盘符（Windows）等极端情况
        raise ValueError(f"Error: 路径 '{path}' 超出当前工作目录")
    if rel == ".." or rel.startswith(".." + os.sep):
        raise ValueError(f"Error: 路径 '{path}' 超出当前工作目录")

    for pat in _load_patterns():
        if re.search(pat, abs_path):
            # 不向模型暴露具体正则，避免泄漏给 LLM
            raise ValueError(f"Error: 拒绝访问 '{path}'")

    return SafePath(wksp=wksp, abs=abs_path, rel=rel)


__all__ = ["safe_path", "SafePath"]
