#!/usr/bin/env python3
"""
路径安全检查模块。

任何接收 ``path`` 参数的工具，都应在处理前调用 :func:`safe_path`
获得规范化后的绝对路径。CWD 之外的路径、或命中
``MYCODE_PROTECTED_PATH_PATTERN`` 环境变量所声明正则的路径，
均被视为不安全，调用方应返回 ``"Error: ..."`` 字符串给模型。

环境变量格式：
    ``MYCODE_PROTECTED_PATH_PATTERN``，逗号分隔的多条正则表达式。
    任一正则命中绝对路径即视为不安全。

软链接会通过 ``os.path.realpath`` 跟随到真实路径后再做安全判断。
"""

import os
import re

PROTECTED_ENV = "MYCODE_PROTECTED_PATH_PATTERN"


def _load_patterns() -> list[str]:
    """读取并切分受保护路径正则列表。"""
    raw = os.getenv(PROTECTED_ENV, "")
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def safe_path(path: str) -> str:
    """规范化路径并验证其安全性。

    :raises ValueError: 路径不安全（越界 CWD 或命中保护正则），
        错误信息以 ``"Error: "`` 开头，便于工具函数直接透传。
    :returns: 规范化后的绝对路径字符串。
    """
    if not path:
        raise ValueError("Error: 路径不能为空")

    # realpath 同时完成绝对化 + 解析 .. + 跟随软链接
    abs_path = os.path.realpath(os.path.abspath(path))
    cwd = os.path.realpath(os.getcwd())

    # 在 CWD 下判定：相对路径若以 ".." 开头即视为越界
    try:
        rel = os.path.relpath(abs_path, cwd)
    except ValueError:
        # 不同盘符（Windows）等极端情况
        raise ValueError(f"Error: 路径 '{path}' 超出当前工作目录")
    if rel == ".." or rel.startswith(".." + os.sep):
        raise ValueError(f"Error: 路径 '{path}' 超出当前工作目录")

    for pat in _load_patterns():
        if re.search(pat, abs_path):
            # 不向模型暴露具体正则，避免泄漏给 LLM
            raise ValueError(f"Error: 拒绝访问 '{path}'")

    return abs_path


__all__ = ["safe_path"]
