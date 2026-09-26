"""
模型提供商配置流程（mycode.provider_setup）测试。

通过 monkeypatch ``ask_ui`` / ``filter_ui`` / ``form_ui`` 返回预设结果，
验证编排分支与写入结果（``providers.load_providers``），不弹真实界面。

覆盖：

- 主菜单选项与取消退出
- 添加 models.dev 模型提供商：候选筛选 → 变量表单 → 模型多选 → 写入
  （密钥变量存 api_key、api 模板变量渲染 base_url；首个提供商设为当前）
- 模型勾选上限（15）：超限截断 / 忽略新增
- 添加自定义模型提供商：name 空回退 base_url host；模型逗号分隔解析；
  模型列表必填：留空不写入
- 编辑：修改变量 / 重选模型 / 删除（当前提供商删除后清空当前模型）
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mycode import config, models_registry as mr, providers as pv, provider_setup as ps


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


def _provider_info(**over):
    base = dict(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com",
        env=["DEEPSEEK_API_KEY"],
        models=["deepseek-chat", "deepseek-reasoner"],
    )
    base.update(over)
    return mr.ProviderInfo(**base)


def _provider_config(**over):
    """构造 ProviderConfig（用于直接写入 providers 测试）。"""
    base = dict(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com",
        api_key="sk-1",
        models=["deepseek-chat", "deepseek-reasoner"],
    )
    base.update(over)
    return pv.ProviderConfig(**base)


def _providers():
    return pv.load_providers()


# ===================================================================
# 主菜单构建
# ===================================================================

class TestMainMenu:
    def test_menu_options_order(self):
        ps._set_meta_status({"updated_at": "2026-09-26T01:00:00+08:00",
                             "status": "success", "providers_count": 182})
        q = ps._main_menu_question([])
        assert q.title == "模型提供商配置"
        opts = q.options or []
        values = [o.effective_value() for o in opts]
        assert values[:2] == ["add_from_catalog", "add_custom"]
        assert values[-1] == "cancel"

    def test_catalog_description_fresh(self):
        ps._set_meta_status({"updated_at": "2026-09-26T01:00:00+08:00",
                             "status": "success", "providers_count": 182})
        q = ps._main_menu_question([])
        assert "182 家" in (q.options[0].description or "")

    def test_catalog_description_error(self):
        ps._set_meta_status({"updated_at": "2026-09-26T01:00:00+08:00",
                             "status": "error", "error": "网络抖动"})
        q = ps._main_menu_question([])
        assert "更新失败" in (q.options[0].description or "")

    def test_catalog_description_not_ready(self):
        ps._set_meta_status({})
        q = ps._main_menu_question([])
        assert "尚未就绪" in (q.options[0].description or "")


# ===================================================================
# 候选列表构建
# ===================================================================

class TestBuildCandidateOptions:
    def test_label_format(self):
        infos = {"deepseek": _provider_info()}
        opts = ps._candidate_options(infos)
        assert opts[0].label == "DeepSeek (deepseek) · 2 模型"
        assert opts[0].value == "deepseek"

    def test_sorted_by_id(self):
        infos = {"b": _provider_info(id="b", name="B"),
                 "a": _provider_info(id="a", name="A")}
        opts = ps._candidate_options(infos)
        assert [o.value for o in opts] == ["a", "b"]


# ===================================================================
# 添加 models.dev 提供商
# ===================================================================

class TestAddFromCatalog:
    def test_full_flow(self, monkeypatch):
        infos = {"deepseek": _provider_info()}
        ps._set_meta_status({"updated_at": "2026-09-26T01:00:00+08:00",
                             "status": "success", "providers_count": 1})
        monkeypatch.setattr(ps, "candidate_providers", lambda: infos)

        # filter 单选 → deepseek；form 变量 → API_KEY；filter 多选 → 模型
        calls = {}

        def fake_filter(choices, **kw):
            calls["filter_kind"] = kw.get("multi")
            if kw.get("multi"):
                return ps.FilterResult(selected=["deepseek-chat", "deepseek-reasoner"])
            return ps.FilterResult(selected=["deepseek"])

        def fake_form(fields, **kw):
            calls["form_fields"] = [f.name for f in fields]
            return ps.FormResult(values={"DEEPSEEK_API_KEY": "sk-x"}, aborted=False)

        monkeypatch.setattr(ps, "filter_ui", fake_filter)
        monkeypatch.setattr(ps, "form_ui", fake_form)
        # 直接从 add 函数调用（跳过主菜单循环）
        ps.add_from_catalog()

        assert calls["form_fields"] == ["DEEPSEEK_API_KEY"]
        p = _providers()["deepseek"]
        assert p.name == "DeepSeek"
        assert p.base_url == "https://api.deepseek.com"
        assert p.api_key == "sk-x"
        assert p.models == ["deepseek-chat", "deepseek-reasoner"]
        # 首个提供商设为当前
        assert pv.get_current() == ("deepseek", "deepseek-chat")

    def test_api_template_substitution(self, monkeypatch):
        infos = {"cf": _provider_info(
            id="cf", name="CF",
            base_url="https://api.x.com/accounts/${ACCOUNT_ID}/v1",
            env=["ACCOUNT_ID", "API_KEY"],
        )}
        ps._set_meta_status({"updated_at": "2026-09-26T01:00:00+08:00",
                             "status": "success", "providers_count": 1})
        monkeypatch.setattr(ps, "candidate_providers", lambda: infos)

        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: ps.FormResult(
                                values={"ACCOUNT_ID": "acc-1", "API_KEY": "sk-cf"}))
        # 第一次 filter（单选候选）返回 cf；第二次（模型多选）返回 [m1]
        def fake_filter(choices, **kw):
            if kw.get("multi"):
                return ps.FilterResult(selected=["m1"])
            return ps.FilterResult(selected=["cf"])

        monkeypatch.setattr(ps, "filter_ui", fake_filter)
        ps.add_from_catalog()
        p = _providers()["cf"]
        assert p.base_url == "https://api.x.com/accounts/acc-1/v1"
        assert p.api_key == "sk-cf"

    def test_abort_form_no_write(self, monkeypatch):
        infos = {"deepseek": _provider_info()}
        ps._set_meta_status({"updated_at": "2026-09-26T01:00:00+08:00",
                             "status": "success", "providers_count": 1})
        monkeypatch.setattr(ps, "candidate_providers", lambda: infos)
        monkeypatch.setattr(ps, "filter_ui",
                            lambda choices, **kw: ps.FilterResult(selected=["deepseek"]))
        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: ps.FormResult(aborted=True))
        ps.add_from_catalog()
        assert _providers() == {}


# ===================================================================
# 模型勾选上限
# ===================================================================

class TestModelCap:
    def test_cap_models(self):
        models = [f"m{i}" for i in range(20)]
        assert ps._cap_models(models) == models[:15]

    def test_full_flow_cap(self, monkeypatch):
        infos = {"big": _provider_info(id="big", name="Big",
                                       models=[f"m{i}" for i in range(20)])}
        ps._set_meta_status({"updated_at": "2026-09-26T01:00:00+08:00",
                             "status": "success", "providers_count": 1})
        monkeypatch.setattr(ps, "candidate_providers", lambda: infos)
        selects = iter([["big"], ["m0", "m1", "m2"]])

        def fake_filter(choices, **kw):
            if kw.get("multi"):
                return ps.FilterResult(selected=["m0", "m1", "m2"])
            return ps.FilterResult(selected=["big"])

        monkeypatch.setattr(ps, "filter_ui", fake_filter)
        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: ps.FormResult(values={}))
        ps.add_from_catalog()
        assert _providers()["big"].models == ["m0", "m1", "m2"]


# ===================================================================
# 添加自定义提供商
# ===================================================================

class TestAddCustom:
    def test_full_flow(self, monkeypatch):
        seen = {}

        def fake_form(fields, **kw):
            seen["fields"] = [f.name for f in fields]
            return ps.FormResult(values={
                "name": "api.openai.com",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-openai",
                "models": "gpt-4o,gpt-4o-mini",
            })

        monkeypatch.setattr(ps, "form_ui", fake_form)
        ps.add_custom()
        pid = "user-defined-1"
        p = _providers()[pid]
        assert p.name == "api.openai.com"
        assert p.base_url == "https://api.openai.com/v1"
        assert p.api_key == "sk-openai"
        assert p.models == ["gpt-4o", "gpt-4o-mini"]

    def test_name_empty_falls_back_to_host(self, monkeypatch):
        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: ps.FormResult(values={
                                "name": "",
                                "base_url": "https://api.openai.com/v1",
                                "api_key": "",
                                "models": "gpt-4o",
                            }))
        ps.add_custom()
        p = _providers()["user-defined-1"]
        assert p.name == "api.openai.com"
        assert p.models == ["gpt-4o"]

    def test_empty_models_rejected(self, monkeypatch):
        """模型列表字段留空：不写入提供商。"""
        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: ps.FormResult(values={
                                "name": "x", "base_url": "https://x/v1",
                                "api_key": "", "models": "",
                            }))
        ps.add_custom()
        assert _providers() == {}


# ===================================================================
# 编辑
# ===================================================================

class TestEdit:
    def test_edit_variables(self, monkeypatch):
        pv.save_provider(_provider_config())
        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: ps.FormResult(values={
                                "base_url": "https://new.example/v1",
                                "api_key": "sk-new",
                                "models": "m1,m2",
                            }))
        ps.edit_variables("deepseek")
        p = _providers()["deepseek"]
        assert p.base_url == "https://new.example/v1"
        assert p.api_key == "sk-new"
        assert p.models == ["m1", "m2"]

    def test_edit_reselect_models(self, monkeypatch):
        pv.save_provider(_provider_config())
        monkeypatch.setattr(ps, "filter_ui",
                            lambda choices, **kw: ps.FilterResult(selected=["m3"]))
        # deepseek 是 models.dev 提供商：需要候选可用
        monkeypatch.setattr(ps, "candidate_providers",
                            lambda: {"deepseek": _provider_info()})
        ps.edit_reselect_models("deepseek")
        assert _providers()["deepseek"].models == ["m3"]

    def test_delete_provider(self, monkeypatch):
        pv.save_provider(_provider_config())
        pv.set_current("deepseek", "deepseek-chat")
        ps.delete_provider("deepseek")
        assert _providers() == {}
        assert pv.get_current() is None

    def test_delete_non_current_keeps_current(self, monkeypatch):
        pv.save_provider(_provider_config(id="a", name="A"))
        pv.save_provider(_provider_config(id="b", name="B"))
        pv.set_current("a", "m")
        ps.delete_provider("b")
        assert pv.get_current() == ("a", "m")