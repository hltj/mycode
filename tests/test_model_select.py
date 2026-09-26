"""
模型切换流程（mycode.model_select）测试。

通过 monkeypatch ``ask_ui`` / ``providers``，验证提供商轮换、选定写回、
取消不改动。

覆盖：

- ``_build_question``：标题位置指示、选项“当前”标注
- ``choose_model``：
  - 无提供商打印提示返回 None
  - 选定模型写回 set_current 且刷新 client
  - 左/右切换提供商重建问题
  - 取消返回 None（不写回）
"""

from __future__ import annotations

import os

import pytest

from mycode import config, models_registry as mr, providers as pv, model_select as ms


@pytest.fixture(autouse=True)
def _setup(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MYCODE_HOME_DIR", str(home))
    for name in list(os.environ):
        if name.startswith("MYCODE_") and name != "MYCODE_HOME_DIR":
            monkeypatch.delenv(name, raising=False)
    cfg = home / "config.toml"
    monkeypatch.setattr(config, "CONFIG_FILE", str(cfg))
    config.invalidate()
    yield
    config.invalidate()


def _pconf(id_, name, models):
    return pv.ProviderConfig(id=id_, name=name, base_url="https://x",
                             api_key="k", models=models)


class TestBuildQuestion:
    def test_title_has_position(self):
        providers = {
            "a": _pconf("a", "A", ["m1", "m2"]),
            "b": _pconf("b", "B", ["m3"]),
        }
        q = ms._build_question(providers, 1)
        assert "(2/2)" in q.title
        assert "B" in q.title

    def test_options_label_and_current_mark(self, monkeypatch):
        providers = {"a": _pconf("a", "A", ["m1", "m2"])}
        monkeypatch.setattr(ms, "get_current", lambda: ("a", "m2"))
        q = ms._build_question(providers, 0)
        labels = [o.label for o in (q.options or [])]
        assert labels == ["m1", "m2（当前）"]


class TestChooseModel:
    def test_no_providers(self, capsys, monkeypatch):
        monkeypatch.setattr(ms, "load_providers", lambda: {})
        assert ms.choose_model() is None
        assert "尚未配置" in capsys.readouterr().out

    def test_select_writes_back_and_refresh(self, monkeypatch):
        monkeypatch.setattr(ms, "load_providers",
                            lambda: {"a": _pconf("a", "A", ["m1", "m2"])})
        monkeypatch.setattr(ms, "get_current", lambda: ("a", "m1"))
        refreshed = []
        monkeypatch.setattr(ms, "refresh_client",
                            lambda: refreshed.append(1))
        # ask_ui 返回选定 m2
        monkeypatch.setattr(ms, "ask_ui", _fake_ask(selected="m2"))
        result = ms.choose_model()
        assert result == ("a", "m2")
        assert pv.get_current() == ("a", "m2")
        assert refreshed == [1]

    def test_cancel_no_write(self, monkeypatch):
        monkeypatch.setattr(ms, "load_providers",
                            lambda: {"a": _pconf("a", "A", ["m1"])})
        monkeypatch.setattr(ms, "get_current", lambda: ("a", "m1"))
        monkeypatch.setattr(ms, "ask_ui", _fake_ask(aborted=True))
        assert ms.choose_model() is None
        assert pv.get_current() is None

    def test_navigate_then_select(self, monkeypatch):
        """左右切换提供商后循环继续：第一次 ask_ui 返回导航空答案，
        choose_model 不退出，第二次选定成功。"""
        providers = {
            "a": _pconf("a", "A", ["a1"]),
            "b": _pconf("b", "B", ["b1"]),
        }
        monkeypatch.setattr(ms, "load_providers", lambda: providers)
        monkeypatch.setattr(ms, "get_current", lambda: ("a", "a1"))
        monkeypatch.setattr(ms, "refresh_client", lambda: None)

        # 模拟真实 ask_ui 的导航行为：第一次调用先触发 on_navigate(+1)
        # （按键右切提供商）再以空答案退出；第二次选定 b1。
        state = {"calls": 0}

        def fake_ask_ui(questions, **kw):
            state["calls"] += 1
            if state["calls"] == 1:
                on_navigate = kw.get("on_navigate")
                assert on_navigate is not None
                assert on_navigate(+1) is True   # 右切到 b
                return _result_of(selected="")   # 导航退出：空答案
            return _result_of(selected="b1")

        monkeypatch.setattr(ms, "ask_ui", fake_ask_ui)
        result = ms.choose_model()
        assert result == ("b", "b1")
        assert pv.get_current() == ("b", "b1")


def _fake_ask(*, selected="", aborted=False):
    from mycode.ask_ui import AskResult, AskAnswer

    def _ask(questions, **kw):
        r = AskResult(aborted=aborted)
        if not aborted:
            r.answers = [AskAnswer(selected=[selected])]
        return r
    return _ask


def _result_of(*, selected="", aborted=False):
    """直接构造 AskResult 实例（selected 空串表示未选定）。"""
    from mycode.ask_ui import AskResult, AskAnswer
    r = AskResult(aborted=aborted)
    if not aborted:
        r.answers = [AskAnswer(selected=[selected] if selected else [])]
    return r
