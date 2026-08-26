"""
pytest 共享配置：autouse fixture 等。

``fake_env`` 为所有测试设置最小环境变量与临时 HOME，避免污染真实环境。
``_clear_rich_caches`` 清掉 rich 内部 ``Style._add`` / ``Color.parse`` /
``Color.downgrade`` 的 ``lru_cache``——这些缓存按 Style / Color 的内容
哈希共享同一个对象，跨测试累积会导致 ``Style._ansi`` 缓存字段混乱
（同一 Style 实例第一次渲染用某种 color_system 设置的 ANSI 码被后续
测试以不同 color_system 渲染时复用，产生跨 color_system 的颜色泄漏）。
``_set_color_system`` 工具 fixture（不在 conftest 直接挂载）由需要的
测试类以 ``autouse=True`` 方式引入，params 自动让该类每个测试在真彩色 /
256 色 / 16 色三种终端能力下都被覆盖到。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def fake_env(monkeypatch, tmp_path):
    """设置最小环境变量 + 临时 HOME，避免污染真实 ~/.mycode"""
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("MODEL_NAME", "test-model")
    monkeypatch.setenv("MYCODE_HOME_DIR", str(tmp_path / ".mycode"))
    yield


@pytest.fixture(autouse=True)
def _clear_rich_caches():
    """每个测试前后清掉 rich 的 lru_cache，避免 Style._ansi 跨测试泄漏。

    详见模块顶部说明。
    """
    from rich.color import Color
    from rich.style import Style

    Style._add.cache_clear()
    Color.parse.cache_clear()
    Color.downgrade.cache_clear()
    yield
    Style._add.cache_clear()
    Color.parse.cache_clear()
    Color.downgrade.cache_clear()


@pytest.fixture(params=("truecolor", "256", "standard"))
def _set_color_system(request, monkeypatch):
    """parametrize 用的工具 fixture：按 param 切换终端颜色能力环境变量。

    用法（在需要的测试类中以 autouse=True 形式引入）：

        class TestFoo:
            @pytest.fixture(autouse=True)
            def _auto_color(self, _set_color_system):
                pass

    每个测试会在 truecolor / 256 色 / 16 色三种终端能力下各验证一次；
    rich 的 Console(force_terminal=True) 会按环境变量自动检测 color_system，
    渲染器与 helper 因此输出对应格式的 ANSI。
    """
    cs = request.param
    if cs == "truecolor":
        monkeypatch.setenv("COLORTERM", "truecolor")
        monkeypatch.delenv("TERM", raising=False)
    elif cs == "256":
        monkeypatch.delenv("COLORTERM", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")
    elif cs == "standard":
        monkeypatch.delenv("COLORTERM", raising=False)
        monkeypatch.setenv("TERM", "xterm")
    else:
        raise ValueError(f"unsupported color_system: {cs!r}")
    return cs
