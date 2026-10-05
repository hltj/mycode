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
import json
from pathlib import Path

import pytest

from mycode import config, models_registry as mr, providers as pv, provider_setup as ps
from mycode.ask_ui import AskAnswer, AskResult


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
        models={"deepseek-chat": mr.ModelInfo(id="deepseek-chat",
                                              name="DeepSeek Chat"),
                "deepseek-reasoner": mr.ModelInfo(id="deepseek-reasoner",
                                                  name="DeepSeek Reasoner")},
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
        assert values[:2] == ["add_from_catalog", "add_user_defined"]
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
        assert opts[0].label == "DeepSeek（deepseek · 2 模型）"
        assert opts[0].value == "deepseek"

    def test_sorted_by_id(self):
        infos = {"b": _provider_info(id="b", name="B"),
                 "a": _provider_info(id="a", name="A")}
        opts = ps._candidate_options(infos)
        assert [o.value for o in opts] == ["a", "b"]

    def test_existing_provider_marked(self):
        """已添加的候选：【已添加】前缀 + {m}/{n} 模型（m=已选个数）。"""
        infos = {"deepseek": _provider_info(
            models={"deepseek-chat": mr.ModelInfo(id="deepseek-chat",
                                                   name="DeepSeek Chat"),
                    "deepseek-reasoner": mr.ModelInfo(id="deepseek-reasoner",
                                                      name="R1")})}
        existing = {"deepseek": _provider_config(models=["deepseek-chat"])}
        opts = ps._candidate_options(infos, existing)
        assert opts[0].label == "DeepSeek（【已添加】deepseek · 1/2 模型）"

    def test_unadded_provider_not_marked(self):
        infos = {"deepseek": _provider_info()}
        opts = ps._candidate_options(infos, {"other": _provider_config()})
        assert "【已添加】" not in opts[0].label


# ===================================================================
# 模型选项构建
# ===================================================================

class TestBuildModelOptions:
    def test_sorted_by_display_name(self):
        """候选模型按显示名排序（不区分大小写），缺失名称回退 id。"""
        models = {"m1": mr.ModelInfo(id="m1", name="DeepSeek V3"),
                  "m2": mr.ModelInfo(id="m2", name="chat-lite"),
                  "m3": mr.ModelInfo(id="m3", name="deepseek r1")}
        opts = ps._model_options(models)
        assert [o.value for o in opts] == ["m2", "m3", "m1"]
        assert opts[0].label == "chat-lite（m2）"

    def test_missing_name_falls_back_to_id(self):
        models = {"b-model": mr.ModelInfo(id="b-model"),
                  "a-model": mr.ModelInfo(id="a-model")}
        opts = ps._model_options(models)
        assert [o.value for o in opts] == ["a-model", "b-model"]
        assert opts[0].label == "a-model（a-model）"


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

    def test_existing_provider_routes_to_edit(self, monkeypatch):
        """选中的候选已添加：不重复添加，进入「编辑：提供商」二级菜单。"""
        pv.save_provider(_provider_config())
        infos = {"deepseek": _provider_info()}
        ps._set_meta_status({"updated_at": "2026-09-26T01:00:00+08:00",
                             "status": "success", "providers_count": 1})
        monkeypatch.setattr(ps, "candidate_providers", lambda: infos)
        monkeypatch.setattr(ps, "filter_ui",
                            lambda choices, **kw: ps.FilterResult(
                                selected=["deepseek"]))
        seen = {}

        def fake_ask(qs, **kw):
            seen["q"] = qs[0]
            return AskResult(answers=[AskAnswer(selected=[ps.EDIT_BACK])])

        monkeypatch.setattr(ps, "ask_ui", fake_ask)
        form_called = []
        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: form_called.append(fields)
                            or ps.FormResult(values={}))
        assert ps.add_from_catalog() == "deepseek"
        # 进入的是编辑二级菜单（非 env 变量表单、非设定值表单）
        assert seen["q"].title == "编辑：DeepSeek"
        assert form_called == []
        # 数据未被改动
        assert _providers()["deepseek"].models == [
            "deepseek-chat", "deepseek-reasoner"]

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
        infos = {"big": _provider_info(
            id="big", name="Big",
            models={f"m{i}": mr.ModelInfo(id=f"m{i}", name=f"Model {i}")
                    for i in range(20)},
        )}
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

class TestAddUserDefined:
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
        ps.add_user_defined()
        pid = "udf-provider-1"
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
        ps.add_user_defined()
        p = _providers()["udf-provider-1"]
        assert p.name == "api.openai.com"
        assert p.models == ["gpt-4o"]

    def test_empty_models_rejected(self, monkeypatch):
        """模型列表字段留空：不写入提供商。"""
        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: ps.FormResult(values={
                                "name": "x", "base_url": "https://x/v1",
                                "api_key": "", "models": "",
                            }))
        ps.add_user_defined()
        assert _providers() == {}


# ===================================================================
# 编辑
# ===================================================================

class TestEdit:
    def test_edit_settings(self, monkeypatch):
        pv.save_provider(_provider_config())
        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: ps.FormResult(values={
                                "name": "新名字",
                                "base_url": "https://new.example/v1",
                                "api_key": "sk-new",
                                "models": "m1,m2",
                            }))
        ps.edit_settings("deepseek")
        p = _providers()["deepseek"]
        assert p.name == "新名字"
        assert p.base_url == "https://new.example/v1"
        assert p.api_key == "sk-new"
        assert p.models == ["m1", "m2"]

    def test_edit_settings_name_empty_keeps_old(self, monkeypatch):
        """显示名留空：回退现有名称，其余照常更新。"""
        pv.save_provider(_provider_config())
        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: ps.FormResult(values={
                                "name": "",
                                "base_url": "https://new.example/v1",
                                "api_key": "sk-new",
                                "models": "m1",
                            }))
        ps.edit_settings("deepseek")
        p = _providers()["deepseek"]
        assert p.name == "DeepSeek"
        assert p.base_url == "https://new.example/v1"

    def test_edit_reselect_models(self, monkeypatch):
        pv.save_provider(_provider_config())
        monkeypatch.setattr(ps, "filter_ui",
                            lambda choices, **kw: ps.FilterResult(selected=["m3"]))
        # deepseek 是 models.dev 提供商：需要候选可用
        monkeypatch.setattr(ps, "candidate_providers",
                            lambda: {"deepseek": _provider_info()})
        ps.edit_reselect_models("deepseek")
        assert _providers()["deepseek"].models == ["m3"]

    def test_custom_edit_menu_has_no_reselect(self):
        """自定义提供商二级菜单：无「重选模型」。"""
        pv.save_provider(_provider_config(id="udf-provider-1", name="X"))
        q = ps._edit_menu_question(
            "udf-provider-1", _providers()["udf-provider-1"])
        values = [o.effective_value() for o in q.options or []]
        assert ps.EDIT_MODELS not in values
        assert values[-1] == ps.EDIT_BACK

    def test_catalog_edit_menu_has_reselect(self):
        """models.dev 提供商二级菜单：含「重选模型」。"""
        pv.save_provider(_provider_config())
        q = ps._edit_menu_question("deepseek", _providers()["deepseek"])
        values = [o.effective_value() for o in q.options or []]
        assert ps.EDIT_MODELS in values
        assert values[0] == ps.EDIT_VARS

    def test_edit_settings_has_provider_level_fields(self, monkeypatch):
        """「修改设定值」表单含提供商级的开关与 extra_body。"""
        pv.save_provider(_provider_config())
        seen = {}
        monkeypatch.setattr(ps, "form_ui", lambda fields, **kw: (
            seen.update(fields=fields),
            ps.FormResult(values={f.name: f.initial for f in fields}))[1])
        ps.edit_settings("deepseek")
        names = [f.name for f in seen["fields"]]
        assert "send_reasoning_content" in names
        assert "extra_body" in names

    def test_edit_settings_placeholder_by_provider_kind(self, monkeypatch):
        """占位文字按提供商区分：自定义提供商不在模型库中，不提模型库。"""
        texts = {}
        for pid in ("deepseek", "udf-provider-1"):
            pv.save_provider(_provider_config(id=pid, name=pid,
                                              models=["m1"]))
            seen = {}
            monkeypatch.setattr(ps, "form_ui", lambda fields, **kw: (
                seen.update(f={x.name: x for x in fields}),
                ps.FormResult(values={x.name: x.initial for x in fields}))[1])
            ps.edit_settings(pid)
            texts[pid] = seen["f"]["send_reasoning_content"].placeholder
        assert texts["deepseek"] == "true/false，留空=按模型库默认"
        assert texts["udf-provider-1"] == "true/false，留空=不回传（false）"

    def test_edit_settings_saves_provider_level(self, monkeypatch):
        """提交后写入提供商级开关与 extra_body。"""
        pv.save_provider(_provider_config())
        monkeypatch.setattr(ps, "form_ui", lambda fields, **kw: ps.FormResult(
            values={**{f.name: f.initial for f in fields},
                    "send_reasoning_content": "true",
                    "extra_body": '{"a": 1}'}))
        ps.edit_settings("deepseek")
        p = pv.load_providers()["deepseek"]
        assert p.send_reasoning_content is True
        assert p.extra_body == {"a": 1}

    def test_edit_settings_blank_clears_provider_level(self, monkeypatch):
        """留空清除提供商级设置（回到后续回退）。"""
        pv.save_provider(_provider_config(send_reasoning_content=True,
                                          extra_body={"a": 1}))
        monkeypatch.setattr(ps, "form_ui", lambda fields, **kw: ps.FormResult(
            values={**{f.name: f.initial for f in fields},
                    "send_reasoning_content": "", "extra_body": ""}))
        ps.edit_settings("deepseek")
        p = pv.load_providers()["deepseek"]
        assert p.send_reasoning_content is None
        assert p.extra_body is None

    def test_edit_settings_echoes_provider_level(self, monkeypatch):
        """已配置的提供商级设置回显到表单。"""
        pv.save_provider(_provider_config(send_reasoning_content=False,
                                          extra_body={"a": 1}))
        seen = {}
        monkeypatch.setattr(ps, "form_ui", lambda fields, **kw: (
            seen.update(fields={f.name: f for f in fields}),
            ps.FormResult(values={f.name: f.initial for f in fields}))[1])
        ps.edit_settings("deepseek")
        assert seen["fields"]["send_reasoning_content"].initial == "false"
        assert '"a": 1' in seen["fields"]["extra_body"].initial

    def test_edit_loop_routes_vars(self, monkeypatch):
        """二级菜单选「修改设定值」走 edit_settings 分支（含改名）。"""
        pv.save_provider(_provider_config(id="udf-provider-1", name="X"))
        answers = iter([
            AskResult(answers=[AskAnswer(selected=[ps.EDIT_VARS])]),
            AskResult(answers=[AskAnswer(selected=[ps.EDIT_BACK])]),
        ])

        def fake_ask(qs, **kw):
            return next(answers)

        monkeypatch.setattr(ps, "ask_ui", fake_ask)
        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: ps.FormResult(values={
                                "name": "Y",
                                "base_url": "https://y.example/v1",
                                "api_key": "",
                                "models": "m1",
                            }))
        ps._run_edit_loop("udf-provider-1")
        p = _providers()["udf-provider-1"]
        assert p.name == "Y"
        assert p.base_url == "https://y.example/v1"

    def test_add_user_defined_with_id_suffix(self, monkeypatch):
        """指定 id 后缀：提供商 id 为 udf-<后缀>。"""
        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: ps.FormResult(values={
                                "id_suffix": "local",
                                "name": "",
                                "base_url": "https://api.openai.com/v1",
                                "api_key": "",
                                "models": "gpt-4o",
                            }))
        assert ps.add_user_defined() == "udf-local"
        assert "udf-local" in _providers()

    def test_add_user_defined_id_suffix_default(self, monkeypatch):
        """后缀默认自动分配（provider-N），留空同样回退自动分配。"""
        seen = {}

        def fake_form(fields, **kw):
            seen["default"] = [f.initial for f in fields
                               if f.name == "id_suffix"][0]
            return ps.FormResult(values={
                "id_suffix": "", "name": "n",
                "base_url": "https://x/v1", "api_key": "",
                "models": "m",
            })

        monkeypatch.setattr(ps, "form_ui", fake_form)
        ps.add_user_defined()
        assert seen["default"] == "provider-1"
        assert "udf-provider-1" in _providers()

    def test_id_suffix_validator_rejects_taken(self):
        """校验器：后缀与现有 id 重复报错；排除自身；留空合法。"""
        pv.save_provider(_provider_config(id="udf-local", name="X",
                                          models=["m"]))
        v = ps._id_suffix_validator()
        assert v("local") == "udf-local 已被占用"
        assert v("free") is None
        assert v("") is None
        # 编辑场景：排除自身后缀
        v2 = ps._id_suffix_validator(exclude_pid="udf-local")
        assert v2("local") is None
        assert v2("localx") is None

    def test_edit_settings_rename_suffix_not_current(self, monkeypatch):
        """编辑设定值改后缀：section 键更新；非当前提供商不动顶层。"""
        pv.save_provider(_provider_config(id="udf-local", name="X",
                                          models=["m"]))
        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: ps.FormResult(values={
                                "name": "X",
                                "id_suffix": "remote",
                                "base_url": "https://x/v1",
                                "api_key": "",
                                "models": "m",
                            }))
        new_id = ps.edit_settings("udf-local")
        assert new_id == "udf-remote"
        assert set(_providers()) == {"udf-remote"}
        assert pv.get_current() is None

    def test_edit_settings_rename_suffix_current_synced(self, monkeypatch):
        """改后缀的是当前提供商：顶层 model_provider 同步新 id。"""
        pv.save_provider(_provider_config(id="udf-local", name="X",
                                          models=["m"]))
        pv.set_current("udf-local", "m")
        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: ps.FormResult(values={
                                "name": "X",
                                "id_suffix": "remote",
                                "base_url": "https://x/v1",
                                "api_key": "",
                                "models": "m",
                            }))
        new_id = ps.edit_settings("udf-local")
        assert new_id == "udf-remote"
        assert set(_providers()) == {"udf-remote"}
        # config 与内存中的当前提供商已同步为新 id
        assert pv.get_current() == ("udf-remote", "m")

    def test_edit_settings_rename_suffix_conflict_keeps_old(self, monkeypatch):
        """目标后缀已被占用：保持原 id，其余字段照常更新。"""
        pv.save_provider(_provider_config(id="udf-a", name="A", models=["m"]))
        pv.save_provider(_provider_config(id="udf-b", name="B", models=["m"]))
        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: ps.FormResult(values={
                                "name": "A",
                                "id_suffix": "b",
                                "base_url": "https://new/v1",
                                "api_key": "",
                                "models": "m",
                            }))
        new_id = ps.edit_settings("udf-a")
        assert new_id == "udf-a"
        assert set(_providers()) == {"udf-a", "udf-b"}
        assert _providers()["udf-a"].base_url == "https://new/v1"

    def test_edit_settings_rename_loop_follows_new_id(self, monkeypatch):
        """二级菜单改后缀后，编辑循环跟随新 id（不退出）。"""
        pv.save_provider(_provider_config(id="udf-local", name="X",
                                          models=["m"]))
        answers = iter([
            AskResult(answers=[AskAnswer(selected=[ps.EDIT_VARS])]),
            AskResult(answers=[AskAnswer(selected=[ps.EDIT_VARS])]),
            AskResult(answers=[AskAnswer(selected=[ps.EDIT_BACK])]),
        ])

        def fake_ask(qs, **kw):
            return next(answers)

        monkeypatch.setattr(ps, "ask_ui", fake_ask)
        calls = []

        def fake_form(fields, **kw):
            calls.append(fields)
            if len(calls) == 1:
                return ps.FormResult(values={
                    "name": "X", "id_suffix": "remote",
                    "base_url": "https://x/v1", "api_key": "", "models": "m",
                })
            return ps.FormResult(values={
                "name": "Y", "id_suffix": "remote",
                "base_url": "https://y/v1", "api_key": "", "models": "m",
            })

        monkeypatch.setattr(ps, "form_ui", fake_form)
        ps._run_edit_loop("udf-local")
        assert set(_providers()) == {"udf-remote"}
        assert _providers()["udf-remote"].name == "Y"

    def test_delete_provider(self, monkeypatch):
        """确认删除：删除并清空当前模型。"""
        pv.save_provider(_provider_config())
        pv.set_current("deepseek", "deepseek-chat")
        monkeypatch.setattr(ps, "ask_ui", lambda qs, **kw: AskResult(
            answers=[AskAnswer(selected=[ps.CONFIRM_DELETE])]))
        assert ps.delete_provider("deepseek") is True
        assert _providers() == {}
        assert pv.get_current() is None

    def test_delete_confirm_default_no(self, monkeypatch):
        """默认不删：取消选项 / 空选择 / Ctrl-C 均不删除。"""
        pv.save_provider(_provider_config())
        pv.set_current("deepseek", "deepseek-chat")

        # 选中「取消删除」（默认光标所在项）
        monkeypatch.setattr(ps, "ask_ui", lambda qs, **kw: AskResult(
            answers=[AskAnswer(selected=[ps.CONFIRM_CANCEL])]))
        assert ps.delete_provider("deepseek") is False
        # 空选择
        monkeypatch.setattr(ps, "ask_ui", lambda qs, **kw: AskResult(
            answers=[AskAnswer(selected=[])]))
        assert ps.delete_provider("deepseek") is False
        # Ctrl-C 中止
        monkeypatch.setattr(ps, "ask_ui", lambda qs, **kw: AskResult(
            aborted=True))
        assert ps.delete_provider("deepseek") is False
        assert _providers()["deepseek"].models == [
            "deepseek-chat", "deepseek-reasoner"]
        assert pv.get_current() == ("deepseek", "deepseek-chat")

    def test_delete_confirm_question(self, monkeypatch):
        """确认问题文案：取消项在前、展示提供商摘要。"""
        pv.save_provider(_provider_config())
        seen = {}

        def fake_ask(qs, **kw):
            seen["q"] = qs[0]
            return AskResult(answers=[AskAnswer(
                selected=[ps.CONFIRM_CANCEL])])

        monkeypatch.setattr(ps, "ask_ui", fake_ask)
        ps.delete_provider("deepseek")
        q = seen["q"]
        assert q.title == "确认删除模型提供商"
        assert q.description == "是否删除模型提供商：DeepSeek（deepseek · 2 模型）"
        assert [o.effective_value() for o in q.options] == [
            ps.CONFIRM_CANCEL, ps.CONFIRM_DELETE]

    def test_delete_non_current_keeps_current(self, monkeypatch):
        pv.save_provider(_provider_config(id="a", name="A"))
        pv.save_provider(_provider_config(id="b", name="B"))
        pv.set_current("a", "m")
        monkeypatch.setattr(ps, "ask_ui", lambda qs, **kw: AskResult(
            answers=[AskAnswer(selected=[ps.CONFIRM_DELETE])]))
        assert ps.delete_provider("b") is True
        assert pv.get_current() == ("a", "m")

    def test_delete_nonexistent(self, monkeypatch):
        """提供商不存在：不询问直接返回。"""
        assert ps.delete_provider("nope") is False

# ===================================================================
# 模型配置（三级菜单 + form_ui）
# ===================================================================

class TestModelConfigMenu:
    """「模型配置」菜单：位置、模型项 label、进入单个模型配置。"""

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        config.CONFIG_FILE = str(tmp_path / "config.toml")
        monkeypatch.setenv("MYCODE_HOME_DIR", str(tmp_path / ".mycode"))
        monkeypatch.setattr(ps, "candidate_providers", lambda: {})
        config.invalidate()

    def test_edit_menu_catalog(self):
        """models.dev 提供商的二级菜单项与顺序。"""
        pv.save_provider(_provider_config())
        q = ps._edit_menu_question("deepseek", _providers()["deepseek"])
        values = [o.effective_value() for o in q.options or []]
        assert values == [ps.EDIT_VARS, ps.EDIT_MODELS, ps.EDIT_MODEL_CONFIG,
                          ps.EDIT_DELETE, ps.EDIT_BACK]

    def test_edit_menu_user_defined(self):
        """自定义提供商的二级菜单项与顺序（无「重选模型」）。"""
        pv.save_provider(_provider_config(id="udf-provider-1", name="X"))
        q = ps._edit_menu_question("udf-provider-1", _providers()["udf-provider-1"])
        values = [o.effective_value() for o in q.options or []]
        assert values == [ps.EDIT_VARS, ps.EDIT_MODEL_CONFIG,
                          ps.EDIT_DELETE, ps.EDIT_BACK]

    def test_model_options_label_name_and_id(self):
        """模型项 label 为「模型名（模型id）」，末项为「返回」。"""
        pv.save_provider(_provider_config())
        existing = _providers()["deepseek"]
        monkey_info = {"deepseek": _provider_info()}
        original = ps.pv.resolve_model_name
        ps.pv.resolve_model_name = lambda pid, m: monkey_info[pid].models[m].name
        try:
            opts = ps._model_config_options(existing, {})
        finally:
            ps.pv.resolve_model_name = original
        labels = [o.label for o in opts]
        assert labels[0] == "DeepSeek Chat（deepseek-chat）"
        assert labels[-1] == "返回"
        assert opts[0].effective_value() == f"{ps.MODEL_CONFIG_PREFIX}deepseek-chat"

    def test_model_options_marks_configured(self):
        """已配置开关/extra_body 的模型在描述里标注「已配置」。"""
        pv.save_provider(_provider_config())
        pv.save_model_config("deepseek",
                             pv.ModelConfig(id="deepseek-chat",
                                            send_reasoning_content=True))
        opts = ps._model_config_options(_providers()["deepseek"],
                                        pv.load_model_configs("deepseek"))
        assert opts[0].description == "已配置"
        assert opts[1].description == ""

    def test_loop_no_models_returns_without_prompt(self, monkeypatch):
        """提供商没有模型时直接返回，不弹空菜单。"""
        pv.save_provider(_provider_config(models=[]))
        called = []
        monkeypatch.setattr(ps, "ask_ui",
                            lambda qs, **kw: called.append(qs) or None)
        ps._run_model_config_loop("deepseek")
        assert called == []

    def test_loop_back_exits(self, monkeypatch):
        """选「返回」退出三级菜单。"""
        pv.save_provider(_provider_config())
        monkeypatch.setattr(ps, "ask_ui", lambda qs, **kw: AskResult(
            answers=[AskAnswer(selected=[ps.MODEL_CONFIG_BACK])]))
        ps._run_model_config_loop("deepseek")

    def test_loop_opens_single_model_form(self, monkeypatch):
        """选某个模型进入该模型的 form_ui 配置，随后回到菜单。"""
        pv.save_provider(_provider_config())
        seen = {"forms": 0}
        # 先选模型进表单，再选「返回」退出循环
        answers = iter([
            AskResult(answers=[AskAnswer(
                selected=[f"{ps.MODEL_CONFIG_PREFIX}deepseek-reasoner"])]),
            AskResult(answers=[AskAnswer(selected=[ps.MODEL_CONFIG_BACK])]),
        ])

        def fake_ask(qs, **kw):
            seen["question"] = qs[0]
            return next(answers)

        def fake_form(fields, **kw):
            seen["fields"] = fields
            seen["title"] = kw.get("title")
            seen["forms"] += 1
            return ps.FormResult(values={f.name: "" for f in fields})

        monkeypatch.setattr(ps, "ask_ui", fake_ask)
        monkeypatch.setattr(ps, "form_ui", fake_form)
        ps._run_model_config_loop("deepseek")
        assert seen["forms"] == 1
        assert [f.name for f in seen["fields"]] == [
            "name", "send_reasoning_content", "extra_body"]
        assert "deepseek-reasoner" in seen["title"]


class TestEditModelForm:
    """单个模型配置表单：初始值、校验、保存。"""

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        config.CONFIG_FILE = str(tmp_path / "config.toml")
        monkeypatch.setenv("MYCODE_HOME_DIR", str(tmp_path / ".mycode"))
        monkeypatch.setattr(ps, "candidate_providers", lambda: {})
        config.invalidate()
        pv.save_provider(_provider_config())

    def _run(self, monkeypatch, values, pid="deepseek", model="deepseek-chat"):
        seen = {}

        def fake_form(fields, **kw):
            seen["fields"] = fields
            return ps.FormResult(values=dict(values))

        monkeypatch.setattr(ps, "form_ui", fake_form)
        ps._edit_model(pid, model)
        return seen["fields"]

    def test_saves_all_three_fields(self, monkeypatch):
        """三项配置写入模型级配置。"""
        self._run(monkeypatch, {
            "name": "我的 DeepSeek",
            "send_reasoning_content": "true",
            "extra_body": '{"chat_template_kwargs": {"thinking": true}}',
        })
        cfg = pv.load_model_configs("deepseek")["deepseek-chat"]
        assert cfg.name == "我的 DeepSeek"
        assert cfg.send_reasoning_content is True
        assert cfg.extra_body == {"chat_template_kwargs": {"thinking": True}}

    def test_form_title_uses_display_name(self, monkeypatch):
        """表单标题为「模型配置：{显式名}（模型id）」。"""
        pv.save_model_config("deepseek", pv.ModelConfig(
            id="deepseek-chat", name="我的 DeepSeek"))
        seen = {}
        monkeypatch.setattr(ps, "form_ui", lambda fields, **kw: (
            seen.update(title=kw.get("title")),
            ps.FormResult(values={f.name: "" for f in fields}))[1])
        ps._edit_model("deepseek", "deepseek-chat")
        assert seen["title"] == "模型配置：我的 DeepSeek（deepseek-chat）"

    def test_form_title_falls_back_when_unnamed(self, monkeypatch):
        """未配置显示名时标题用模型库名称。"""
        seen = {}
        monkeypatch.setattr(ps.pv, "resolve_model_name",
                            lambda pid, m: "DeepSeek Chat")
        monkeypatch.setattr(ps, "form_ui", lambda fields, **kw: (
            seen.update(title=kw.get("title")),
            ps.FormResult(values={f.name: "" for f in fields}))[1])
        ps._edit_model("deepseek", "deepseek-chat")
        assert seen["title"] == "模型配置：DeepSeek Chat（deepseek-chat）"

    def test_flag_true_false_parsing(self, monkeypatch):
        """开关按布尔解析 true/false（大小写不敏感）。"""
        for text, expected in (("true", True), ("TRUE", True), ("True", True),
                               ("false", False), ("FALSE", False),
                               ("  true  ", True)):
            self._run(monkeypatch, {"name": "", "send_reasoning_content": text,
                                    "extra_body": ""})
            cfg = pv.load_model_configs("deepseek")["deepseek-chat"]
            assert cfg.send_reasoning_content is expected

    def test_blank_flag_removes_override(self, monkeypatch):
        """留空表示回到「按模型库推导」，该键被删除。"""
        pv.save_model_config("deepseek", pv.ModelConfig(
            id="deepseek-chat", send_reasoning_content=True, name="留着名字"))
        self._run(monkeypatch, {"name": "留着名字", "send_reasoning_content": "",
                                "extra_body": ""})
        cfg = pv.load_model_configs("deepseek")["deepseek-chat"]
        assert cfg.send_reasoning_content is None
        assert cfg.name == "留着名字"

    def test_all_blank_removes_entry(self, monkeypatch):
        """三项全留空：模型配置项整体删除。"""
        pv.save_model_config("deepseek", pv.ModelConfig(
            id="deepseek-chat", send_reasoning_content=True))
        self._run(monkeypatch, {"name": "", "send_reasoning_content": "",
                                "extra_body": ""})
        assert "deepseek-chat" not in pv.load_model_configs("deepseek")

    def test_explicit_flag_echoed_back(self, monkeypatch):
        """已显式配置的开关在表单里回显。"""
        pv.save_model_config("deepseek", pv.ModelConfig(
            id="deepseek-chat", send_reasoning_content=True))
        fields = self._run(monkeypatch, {"name": "", "send_reasoning_content": "",
                                         "extra_body": ""})
        assert fields[1].initial == "true"

    def test_unset_flag_blank_with_placeholder_hint(self, monkeypatch):
        """未配置时初始值留空，placeholder 提示按模型库推导的结果。"""
        monkeypatch.setattr(ps, "pv", ps.pv)
        monkeypatch.setattr(ps.pv, "resolve_send_reasoning",
                            lambda pid, m: True)
        fields = self._run(monkeypatch, {"name": "", "send_reasoning_content": "",
                                         "extra_body": ""})
        assert fields[1].initial == ""
        assert "留空=按模型库默认（true）" in fields[1].placeholder

    def test_extra_body_echoed_single_line(self, monkeypatch):
        """已配置的 extra_body 回显为单行 JSON。

        form_ui 是单行输入，多行 JSON 只会显示末行（曾导致回显只剩 `}`），
        因此这里断言不含换行且能原样解析回原对象。
        """
        raw = '{"enable_thinking": true, "preserve_thinking": true}'
        pv.save_model_config("deepseek", pv.ModelConfig(
            id="deepseek-chat", extra_body=json.loads(raw)))
        fields = self._run(monkeypatch, {"name": "", "send_reasoning_content": "",
                                         "extra_body": raw})
        initial = fields[2].initial
        assert chr(0x0A) not in initial
        assert initial == raw
        assert json.loads(initial) == {"enable_thinking": True,
                                       "preserve_thinking": True}

    def test_aborted_form_no_write(self, monkeypatch):
        """取消（aborted）时不写任何配置。"""
        pv.save_model_config("deepseek", pv.ModelConfig(
            id="deepseek-chat", name="原有"))
        monkeypatch.setattr(ps, "form_ui", lambda fields, **kw: ps.FormResult(
            aborted=True))
        ps._edit_model("deepseek", "deepseek-chat")
        assert pv.load_model_configs("deepseek")["deepseek-chat"].name == "原有"

    def test_unknown_model_no_form(self, monkeypatch):
        """模型不在该提供商的模型列表里：不弹表单。"""
        called = []
        monkeypatch.setattr(ps, "form_ui",
                            lambda fields, **kw: called.append(fields))
        ps._edit_model("deepseek", "not-configured")
        assert called == []

    def test_field_hints_and_placeholder(self, monkeypatch):
        """字段说明文案：开关说明与 extra_body 占位/提示。"""
        fields = self._run(monkeypatch, {"name": "", "send_reasoning_content": "",
                                         "extra_body": ""})
        assert fields[1].hint == "历史思考随历史消息发回模型"
        assert fields[2].placeholder == \
            '如 DeepSeek 配置 {"thinking": {"type": "enabled"}}' 
        assert fields[2].hint == "JSON 对象"

    def test_switch_placeholder_follows_provider_level(self, monkeypatch):
        """提供商级配了开关时，占位文字提示「按本提供商默认」。"""
        pv.save_provider(_provider_config(send_reasoning_content=True))
        fields = self._run(monkeypatch, {"name": "", "send_reasoning_content": "",
                                         "extra_body": ""})
        assert fields[1].placeholder == "true/false，留空=按本提供商默认（true）"

    def test_switch_placeholder_model_library_when_no_provider_flag(self, monkeypatch):
        """提供商级未配开关时，仍提示「按模型库默认」。"""
        fields = self._run(monkeypatch, {"name": "", "send_reasoning_content": "",
                                         "extra_body": ""})
        assert "按模型库默认" in fields[1].placeholder

    def test_udf_wording_falls_back_to_model_id(self, monkeypatch):
        """自定义提供商：显示名提示改用模型 id，开关措辞与目录一致。"""
        pv.save_provider(_provider_config(id="udf-provider-1", name="Local",
                                          models=["my-model"]))
        seen = {}

        def fake_form(fields, **kw):
            seen["fields"] = fields
            return ps.FormResult(values={f.name: "" for f in fields})

        monkeypatch.setattr(ps, "form_ui", fake_form)
        ps._edit_model("udf-provider-1", "my-model")
        name_f, flag_f = seen["fields"][0], seen["fields"][1]
        assert name_f.hint == "留空则用模型 id"
        assert name_f.placeholder == "my-model"
        # 无模型库数据 → 推导值恒为 false，在「不回传」措辞后追加（false）
        assert flag_f.placeholder == "true/false，留空=不回传（false）"
        assert flag_f.hint == "历史思考随历史消息发回模型"

    def test_switch_placeholder_keeps_own_wording(self, monkeypatch):
        """两种提供商的开关占位措辞各自不同，但都带推导值。"""
        pv.save_provider(_provider_config(id="udf-provider-1", name="Local",
                                          models=["my-model"]))
        catalog = self._run(monkeypatch, {
            "name": "", "send_reasoning_content": "", "extra_body": ""},
            pid="deepseek", model="deepseek-chat")[1].placeholder
        udf = self._run(monkeypatch, {
            "name": "", "send_reasoning_content": "", "extra_body": ""},
            pid="udf-provider-1", model="my-model")[1].placeholder
        assert catalog == "true/false，留空=按模型库默认（false）"
        assert udf == "true/false，留空=不回传（false）"

    def test_catalog_wording_mentions_model_library(self, monkeypatch):
        """models.dev 提供商：显示名提示模型库名称。"""
        fields = self._run(monkeypatch, {"name": "", "send_reasoning_content": "",
                                         "extra_body": ""})
        assert fields[0].hint == "留空则用模型库名称"
        assert "按模型库默认" in fields[1].placeholder

    def test_field_validators(self, monkeypatch):
        """开关与 extra_body 字段的校验器行为。"""
        fields = self._run(monkeypatch, {"name": "", "send_reasoning_content": "",
                                         "extra_body": ""})
        flag_v = fields[1].validator
        assert flag_v("true") is None
        assert flag_v("false") is None
        assert flag_v("") is None
        assert flag_v("是") == "请填 true 或 false"
        body_v = fields[2].validator
        assert body_v('{"a":1}') is None
        assert body_v("") is None
        assert "不是合法 JSON" in body_v("{bad")
        assert "必须是 JSON 对象" in body_v("[1]")


class TestParseBoolHelper:
    @pytest.mark.parametrize("text", ["true", "TRUE", "True", " true "])
    def test_true(self, text):
        assert ps._parse_bool(text) == (True, None)

    @pytest.mark.parametrize("text", ["false", "FALSE", "False", " false "])
    def test_false(self, text):
        assert ps._parse_bool(text) == (False, None)

    @pytest.mark.parametrize("text", ["", "   "])
    def test_blank_is_none(self, text):
        """留空表示回到「按模型库推导」。"""
        assert ps._parse_bool(text) == (None, None)

    @pytest.mark.parametrize("text", ["是", "yes", "1", "瞎写"])
    def test_invalid_reports_error(self, text):
        """非 TOML 布尔字面量一律报错（不做宽松匹配）。"""
        flag, err = ps._parse_bool(text)
        assert flag is None
        assert err == "请填 true 或 false"
