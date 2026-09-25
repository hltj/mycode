"""
配置读取模块。

除 ``MYCODE_HOME_DIR`` 之外的所有 ``MYCODE_*`` 配置项统一从本模块读取。
调用方式：传小写且不含 ``MYCODE_`` 前缀的 key，如 ``config.get("bash_timeout", 60)``。

取值优先级：

1. 环境变量（含 ``.env`` 加载进来的值），key 转为大写并加 ``MYCODE_`` 前缀；
2. ``{MYCODE_HOME_DIR}/config.toml`` 中的同名键（就用这个小写 key）；
3. 调用方传入的默认值。

数组类配置（如 ``bash_dangerous``、``e429_wait_seconds``）在 TOML 中
配置为数组（``e429_wait_seconds = [1, 2, 5, 10]``）；环境变量来源仍为
逗号分隔字符串（环境变量无法表达数组），两种来源最终都解析为同一种
Python 类型返回。

``MYCODE_API_KEY`` 允许写进 config.toml（该文件位于用户私有目录），
``cli`` 读取后同样做阅后即焚处理（从环境变量中移除，见 cli.py）。
"""

from __future__ import annotations

import os

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from typing import cast

# 配置文件路径只在导入时确定一次：依赖 MYCODE_HOME_DIR 环境变量，
# 因此本模块必须先于任何依赖 MYCODE_HOME_DIR 的模块导入（cli 中
# load_dotenv() 之后立刻导入本模块）。
CONFIG_FILE = os.path.join(
    os.path.expanduser(os.getenv('MYCODE_HOME_DIR', os.path.expanduser('~/.mycode'))),
    "config.toml",
)

_raw_cache: dict | None = None
_raw_loaded = False


def _raw_config() -> dict:
    """读取并缓存 config.toml 顶层表（文件不存在 / 解析失败返回空 dict）。

    解析失败（语法错误）时打印警告到 stderr 并忽略整个文件，
    避免一次手误让 CLI 完全起不来。
    """
    global _raw_cache, _raw_loaded
    if _raw_loaded:
        return _raw_cache or {}
    _raw_loaded = True
    try:
        with open(CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        _raw_cache = None
        return {}
    except tomllib.TOMLDecodeError as e:
        import sys
        print(f"警告: 配置文件 {CONFIG_FILE} 解析失败，已忽略: {e}", file=sys.stderr)
        _raw_cache = None
        return {}
    except OSError as e:
        print(f"警告: 配置文件 {CONFIG_FILE} 读取失败，已忽略: {e}", file=sys.stderr)
        _raw_cache = None
        return {}
    _raw_cache = data if isinstance(data, dict) else None
    return _raw_cache or {}


def _lookup(key: str, split_env: bool = False):
    """按优先级取原始值：环境变量 > config.toml；取不到返回 None。

    环境变量名 = ``MYCODE_`` + key 大写；TOML 键用 key 本身（小写）。
    空字符串视为未设置。``split_env=True`` 时环境变量字符串按逗号切分
    为列表返回（保留空段，供调用方识别非法），与 TOML 数组形态对齐
    （环境变量无法表达数组）。
    """
    env = os.getenv("MYCODE_" + key.upper())
    if env is not None and env != "":
        return env.split(",") if split_env else env
    v = _raw_config().get(key)
    if v == "":
        return None
    return v


def get(key: str, default=None):
    """取字符串配置项（TOML 中非字符串的值转为字符串）。"""
    v = _lookup(key)
    if v is None:
        return default
    return v if isinstance(v, str) else str(v)


def get_int(key: str, default: int) -> int:
    """取整数配置项（无法解析时回退 default）。"""
    v = _lookup(key)
    if isinstance(v, list):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def get_list(key: str) -> list[str] | None:
    """取字符串列表（TOML 数组；环境变量按逗号切分）；未配置返回 None。"""
    v = _lookup(key, split_env=True)
    if v is None:
        return None
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v).strip()] if str(v).strip() else None


def _to_int(value: object) -> int | None:
    """单项转正整数：非法（空 / 非整数 / 非正数）返回 None。"""
    s = str(value).strip()
    if not s:
        return None
    try:
        n = int(s)
    except ValueError:
        return None
    return n if n > 0 else None


def get_int_list(key: str) -> list[int] | None:
    """取正整数列表（TOML 数组；环境变量按逗号切分），任一项非法则返回 None。"""
    v = _lookup(key, split_env=True)
    if not isinstance(v, list) or not v:
        return None
    parsed = [_to_int(x) for x in v]
    return None if None in parsed else cast(list[int], parsed)
