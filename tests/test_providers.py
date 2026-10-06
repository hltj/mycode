"""
提供商配置模块（mycode.providers）测试。

覆盖：

- ``ProviderConfig`` 数据
- ``save_provider`` / ``load_providers`` / ``delete_provider``：保注释读写、
  更新已有 section、删除
- ``set_current`` / ``get_current`` / ``is_configured``
- ``next_user_defined_id``：从小到大取未占用的 udf-provider-N
- ``migrate_legacy``：
  - 顶层 api_key/base_url（任一非空）迁移为 [providers.udf-provider-N]，
    name 取 base_url host，models 取旧 model_name
  - 写 model_provider / model；删除顶层 api_key/base_url
  - 环境变量 MYCODE_API_KEY 等不参与迁移
  - 无旧配置时返回 None
  - 已有 udf-provider-N 时取未占用下一个
- 写回后 config 缓存失效（config.get 能读到新写 model_provider）
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mycode import config, providers as pv


@pytest.fixture(autouse=True)
def _setup(monkeypatch, tmp_path):
    """每个测试用独立 config.toml，并清掉除 HOME 外的 MYCODE_* 环境变量。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MYCODE_HOME_DIR", str(home))
    for name in list(os.environ):
        if name.startswith("MYCODE_") and name != "MYCODE_HOME_DIR":
            monkeypatch.delenv(name, raising=False)
    cfg = home / "config.toml"
    monkeypatch.setattr(config, "CONFIG_FILE", str(cfg))
    config._raw_cache = None
    config._raw_loaded = False
    yield
    config._raw_cache = None
    config._raw_loaded = False


def _config_path() -> Path:
    return Path(config.CONFIG_FILE)


def _write_config(text: str) -> None:
    _config_path().write_text(text, encoding="utf-8")


def _read_config() -> str:
    return _config_path().read_text(encoding="utf-8")


def _provider(**over):
    base = dict(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com",
        api_key="sk-1",
        models=["deepseek-chat", "deepseek-reasoner"],
    )
    base.update(over)
    return pv.ProviderConfig(**base)


# ===================================================================
# ProviderConfig
# ===================================================================

class TestProviderConfig:
    def test_defaults(self):
        p = pv.ProviderConfig(id="x", name="X", base_url="https://x", api_key="", models=[])
        assert p.models == []


# ===================================================================
# save / load / delete
# ===================================================================

BASE_DOC = """\
# 顶部注释
syntax_theme = "nord"

[providers.deepseek]
name = "DeepSeek"
base_url = "https://api.deepseek.com"
api_key = "sk-1"
enabled_models = ["deepseek-chat"]
"""


class TestSaveLoad:
    def test_save_preserves_existing_comment(self):
        _write_config(BASE_DOC)
        pv.save_provider(_provider())
        text = _read_config()
        assert "# 顶部注释" in text
        assert 'syntax_theme = "nord"' in text
        assert '[providers.deepseek]' in text

    def test_load_providers(self):
        _write_config(BASE_DOC)
        ps = pv.load_providers()
        assert set(ps) == {"deepseek"}
        p = ps["deepseek"]
        assert p.id == "deepseek"
        assert p.name == "DeepSeek"
        assert p.base_url == "https://api.deepseek.com"
        assert p.api_key == "sk-1"
        assert p.models == ["deepseek-chat"]

    def test_save_new_provider_appends_section(self):
        _write_config(BASE_DOC)
        pv.save_provider(_provider(id="zhipu", name="Zhipu", base_url="https://open.bigmodel.cn/api/paas/v4"))
        ps = pv.load_providers()
        assert set(ps) == {"deepseek", "zhipu"}

    def test_update_existing_provider_preserves_other(self):
        _write_config(BASE_DOC)
        pv.save_provider(_provider(api_key="sk-2", models=["deepseek-chat", "new-model"]))
        text = _read_config()
        assert "# 顶部注释" in text
        assert 'api_key = "sk-2"' in text
        assert '"new-model"' in text
        assert '"deepseek-reasoner"' not in text

    def test_delete_provider(self):
        _write_config(BASE_DOC)
        pv.delete_provider("deepseek")
        assert pv.load_providers() == {}
        assert '[providers.deepseek]' not in _read_config()

    def test_load_empty_missing_file(self):
        assert pv.load_providers() == {}

    def test_invalid_toml_returns_empty(self, capsys):
        _write_config("this is = = not valid [[[\n")
        assert pv.load_providers() == {}


# ===================================================================
# current model
# ===================================================================

class TestCurrent:
    def test_get_current_none(self):
        _write_config("")
        assert pv.get_current() is None

    def test_set_and_get_current(self):
        _write_config(BASE_DOC)
        pv.set_current("deepseek", "deepseek-reasoner")
        assert pv.get_current() == ("deepseek", "deepseek-reasoner")
        text = _read_config()
        assert 'model_provider = "deepseek"' in text
        assert 'model = "deepseek-reasoner"' in text

    def test_current_persists_comment(self):
        _write_config(BASE_DOC)
        pv.set_current("deepseek", "deepseek-chat")
        assert "# 顶部注释" in _read_config()

    def test_is_configured(self):
        assert pv.is_configured() is False
        pv.save_provider(_provider())
        assert pv.is_configured() is True


# ===================================================================
# udf-provider id 分配
# ===================================================================

class TestNextUserDefined:
    def test_first_is_1(self):
        _write_config("")
        assert pv.next_user_defined_id() == "udf-provider-1"

    def test_skips_existing(self):
        _write_config(
            '[providers.udf-provider-1]\nname="a"\n\n'
            '[providers.udf-provider-3]\nname="b"\n'
        )
        assert pv.next_user_defined_id() == "udf-provider-2"


# ===================================================================
# 旧配置迁移
# ===================================================================

class TestMigrateLegacy:
    def test_migrates_full(self):
        _write_config(
            'api_key = "legacy-key"\n'
            'base_url = "https://api.openai.com/v1"\n'
            'model_name = "gpt-4o"\n'
            '# comment\n'
            'syntax_theme = "nord"\n'
        )
        pid = pv.migrate_legacy()
        assert pid == "udf-provider-1"
        ps = pv.load_providers()
        assert set(ps) == {"udf-provider-1"}
        p = ps["udf-provider-1"]
        assert p.name == "api.openai.com"
        assert p.base_url == "https://api.openai.com/v1"
        assert p.api_key == "legacy-key"
        assert p.models == ["gpt-4o"]
        assert pv.get_current() == ("udf-provider-1", "gpt-4o")
        text = _read_config()
        # 顶层 legacy 的 base_url / api_key 已删（provider 内的同名键不受
        # 影响，不能简单断言整文不含 base_url）
        import re
        top = text.split('[providers.')[0]
        assert 'base_url = ' not in top
        assert 'api_key = ' not in top
        assert '# comment' in text
        assert 'syntax_theme = "nord"' in text

    def test_migrates_base_url_only(self):
        _write_config('base_url = "http://127.0.0.1:1234/v1"\n')
        pid = pv.migrate_legacy()
        assert pid == "udf-provider-1"
        p = pv.load_providers()["udf-provider-1"]
        assert p.base_url == "http://127.0.0.1:1234/v1"
        assert p.api_key == ""
        assert p.models == []

    def test_ignores_env_vars(self, monkeypatch):
        _write_config('base_url = "https://api.openai.com/v1"\n')
        monkeypatch.setenv("MYCODE_API_KEY", "from-env")
        monkeypatch.setenv("MYCODE_BASE_URL", "https://env.example/v1")
        monkeypatch.setenv("MYCODE_MODEL_NAME", "env-model")
        pid = pv.migrate_legacy()
        assert pid == "udf-provider-1"
        p = pv.load_providers()[pid]
        assert p.base_url == "https://api.openai.com/v1"
        assert p.api_key == ""
        assert p.models == []
        assert p.name == "api.openai.com"

    def test_no_legacy_returns_none(self):
        _write_config('# only comment\nsyntax_theme = "nord"\n')
        assert pv.migrate_legacy() is None

    def test_existing_provider_preserved_and_next_id(self):
        _write_config(
            'api_key = "legacy"\n'
            'base_url = "https://api.example.com/v1"\n'
            'model_name = "m1"\n'
            '[providers.udf-provider-1]\n'
            'name = "existing"\n'
        )
        pid = pv.migrate_legacy()
        assert pid == "udf-provider-2"
        assert set(pv.load_providers()) == {"udf-provider-1", "udf-provider-2"}


# ===================================================================
# 写回后 config 缓存失效
# ===================================================================

class TestConfigInvalidation:
    def test_set_current_seen_by_config_get(self):
        _write_config(BASE_DOC)
        pv.set_current("deepseek", "deepseek-reasoner")
        assert config.get("model_provider") == "deepseek"
        assert config.get("model") == "deepseek-reasoner"

    def test_set_current_seen_by_config_get_after_second_write(self):
        _write_config(BASE_DOC)
        pv.set_current("deepseek", "a")
        config.get("model")  # 触发一次缓存
        pv.set_current("deepseek", "b")
        assert config.get("model") == "b"

# ===================================================================
# 模型级配置（[providers.<id>.model_config.<model>]）
# ===================================================================

class TestModelConfig:
    """模型级配置：显示名 / 回传 reasoning_content / extra_body。"""

    @pytest.fixture
    def cfg_file(self, tmp_path, monkeypatch):
        """指向临时 config.toml。"""
        from mycode import config
        path = tmp_path / "config.toml"
        monkeypatch.setattr(config, "CONFIG_FILE", str(path))
        return path

    @pytest.fixture
    def provider(self, cfg_file):
        from mycode.providers import ProviderConfig, save_provider
        save_provider(ProviderConfig(
            id="p1", name="P1", base_url="https://x/v1", api_key="k",
            models=["m1", "vendor/model-2"],
        ))
        return "p1"

    def test_save_and_load_roundtrip(self, provider):
        """模型配置写入后可读回（含 extra_body dict）。"""
        from mycode.providers import ModelConfig, load_model_configs, save_model_config
        save_model_config(provider, ModelConfig(
            id="m1", name="我的模型", send_reasoning_content=True,
            extra_body={"a": 1, "b": {"c": 2}}))
        got = load_model_configs(provider)
        assert got["m1"] == ModelConfig(
            id="m1", name="我的模型", send_reasoning_content=True,
            extra_body={"a": 1, "b": {"c": 2}})

    def test_model_id_with_slash_and_dot(self, cfg_file, provider):
        """模型 id 含 / 与 . 时键能正确加引号写入并读回。"""
        from mycode.providers import ModelConfig, load_model_configs, save_model_config
        save_model_config(provider, ModelConfig(id="vendor/model-2", name="带斜杠"))
        assert load_model_configs(provider)["vendor/model-2"].name == "带斜杠"
        assert '[providers.p1.models."vendor/model-2"]' in \
            open(cfg_file, encoding="utf-8").read()

    def test_flag_omitted_means_unset(self, provider):
        """未设置开关时为 None（由 models.dev 推导，而非 False）。"""
        from mycode.providers import ModelConfig, load_model_configs, save_model_config
        save_model_config(provider, ModelConfig(id="m1", name="只有名字"))
        assert load_model_configs(provider)["m1"].send_reasoning_content is None

    def test_flag_false_persisted(self, provider):
        """显式 False 与「未配置」不同，能持久化。"""
        from mycode.providers import ModelConfig, load_model_configs, save_model_config
        save_model_config(provider, ModelConfig(id="m1", send_reasoning_content=False))
        assert load_model_configs(provider)["m1"].send_reasoning_content is False

    def test_empty_name_not_written(self, cfg_file, provider):
        """显示名为空时不写 name 键（不留 name = "" 噪音）。"""
        from mycode.providers import ModelConfig, load_model_configs, save_model_config
        save_model_config(provider, ModelConfig(id="m1", name="",
                                                send_reasoning_content=True))
        body = open(cfg_file, encoding="utf-8").read()
        assert "name =" not in body.split("models.m1]")[1]
        assert load_model_configs(provider)["m1"].name == ""

    def test_only_written_fields_present(self, cfg_file, provider):
        """只写实际配置的字段：设 name 不写开关 / extra_body。"""
        from mycode.providers import ModelConfig, save_model_config
        save_model_config(provider, ModelConfig(id="m1", name="只有名字"))
        section = open(cfg_file, encoding="utf-8").read().split("models.m1]")[1]
        assert 'name = "只有名字"' in section
        assert "send_reasoning_content" not in section
        assert "extra_body" not in section

    def test_all_empty_removes_entry(self, provider):
        """三字段全空时删除该模型子表，不留空配置。"""
        from mycode.providers import ModelConfig, load_model_configs, save_model_config
        save_model_config(provider, ModelConfig(id="m1", name="x"))
        assert "m1" in load_model_configs(provider)
        save_model_config(provider, ModelConfig(id="m1"))
        assert "m1" not in load_model_configs(provider)

    def test_invalid_extra_body_json_dropped(self, provider, cfg_file):
        """extra_body 非法 JSON 时读回为 None（写入侧已校验，此处模拟脏数据）。"""
        cfg_file.write_text(
            '[providers.p1.models.m1]\nextra_body = "{bad json"\n',
            encoding="utf-8")
        from mycode.providers import load_model_configs
        assert load_model_configs(provider)["m1"].extra_body is None

    def test_enabled_models_key_used(self, cfg_file, provider):
        """模型 id 列表写在 enabled_models 键下（models 已被模型级配置占用）。"""
        body = open(cfg_file, encoding="utf-8").read()
        assert 'enabled_models = ["m1", "vendor/model-2"]' in body
        assert pv.load_providers()["p1"].models == ["m1", "vendor/model-2"]

    def test_save_provider_writes_enabled_models(self, cfg_file, provider):
        """save_provider 写 enabled_models，且不覆盖 models 子表。"""
        pv.save_model_config("p1", pv.ModelConfig(id="m1", send_reasoning_content=True))
        pv.save_provider(pv.ProviderConfig(id="p1", name="P", base_url="u",
                                           api_key="k", models=["m1"]))
        body = open(cfg_file, encoding="utf-8").read()
        assert "enabled_models = [\"m1\"]" in body
        assert "send_reasoning_content = true" in body

    def test_load_unknown_provider(self, cfg_file):
        """提供商不存在时返回空 dict。"""
        from mycode.providers import load_model_configs
        assert load_model_configs("nope") == {}

    def test_save_unknown_provider_noop(self, cfg_file):
        """提供商不存在时 save 不写盘。"""
        from mycode.providers import ModelConfig, save_model_config
        save_model_config("nope", ModelConfig(id="m1", name="x"))
        assert not cfg_file.exists() or "nope" not in cfg_file.read_text(encoding="utf-8")

    def test_save_provider_preserves_model_config(self, provider):
        """编辑提供商设定值不清掉模型级配置。"""
        from mycode.providers import (ModelConfig, ProviderConfig, load_model_configs,
                                      load_providers, save_model_config, save_provider)
        save_model_config(provider, ModelConfig(id="m1", name="保留我",
                                                send_reasoning_content=True))
        save_provider(ProviderConfig(id="p1", name="改名", base_url="https://y/v1",
                                     api_key="k2", models=["m1"]))
        assert load_providers()["p1"].name == "改名"
        cfg = load_model_configs(provider)["m1"]
        assert cfg.name == "保留我" and cfg.send_reasoning_content is True

    def test_delete_provider_removes_model_config(self, provider):
        """删除提供商时其模型配置一并消失。"""
        from mycode.providers import (ModelConfig, delete_provider, load_model_configs,
                                      save_model_config)
        save_model_config(provider, ModelConfig(id="m1", name="x"))
        delete_provider(provider)
        assert load_model_configs(provider) == {}


class TestResolveModelSettings:
    """回传开关 / extra_body / 显示名的解析优先级。"""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        """临时 home：config.toml 与 models.dev 缓存都落在这里。

        models_registry 的缓存目录按 ``MYCODE_HOME_DIR`` 环境变量在调用时
        解析（不在导入时固定），因此两个来源都要指向同一个 home。
        """
        from mycode import config
        home = tmp_path / ".mycode"
        monkeypatch.setenv("MYCODE_HOME_DIR", str(home))
        monkeypatch.setattr(config, "CONFIG_FILE", str(home / "config.toml"))
        (home / "models_cache").mkdir(parents=True)
        return home

    @staticmethod
    def _write_cache(home, api: dict) -> None:
        import json
        (home / "models_cache" / "api.json").write_text(
            json.dumps(api), encoding="utf-8")

    @staticmethod
    def _api() -> dict:
        return {"acme": {"npm": "@ai-sdk/openai-compatible", "name": "Acme",
                         "api": "https://a/v1", "env": ["API_KEY"], "models": {
            "m-r1": {"id": "m-r1", "name": "R1",
                     "interleaved": {"field": "reasoning_content"}},
            "m-true": {"id": "m-true", "name": "T", "interleaved": True},
            "m-det": {"id": "m-det", "name": "D",
                      "interleaved": {"field": "reasoning_details"}},
            "m-none": {"id": "m-none", "name": "N"},
        }}}

    @pytest.fixture
    def provider(self, env):
        from mycode.providers import ProviderConfig, save_provider
        save_provider(ProviderConfig(id="acme", name="Acme", base_url="u",
                                     api_key="k",
                                     models=["m-r1", "m-true", "m-det", "m-none"]))
        return "acme"

    @pytest.fixture
    def default_pid(self, monkeypatch) -> str:
        """装一张只含单个提供商预置记录的表，返回该提供商 id。

        生产表会随内置厂商增减，故用固定的测试表而非依赖真实条目。
        """
        from mycode import provider_presets
        t = [provider_presets.ProviderPreset(
            id="test-def", name="测试默认",
            for_providers=("test-def-p",),
            config=provider_presets.PresetConfig(
                send_reasoning_content=True,
                extra_body={"thinking": {"type": "enabled"}}))]
        monkeypatch.setattr(provider_presets, "_PROVIDER_PRESETS", t)
        monkeypatch.setattr(provider_presets, "_BY_PROVIDER",
                            provider_presets._rebuild_index(t))
        return "test-def-p"

    def test_default_from_interleaved_field(self, env, provider):
        """interleaved.field == reasoning_content → 默认回传。"""
        from mycode.providers import resolve_send_reasoning
        self._write_cache(env, self._api())
        assert resolve_send_reasoning("acme", "m-r1") is True

    def test_default_other_interleaved_forms(self, env, provider):
        """interleaved 为 true / 其他字段 / 缺失 → 默认不回传。"""
        from mycode.providers import resolve_send_reasoning
        self._write_cache(env, self._api())
        for m in ("m-true", "m-det", "m-none"):
            assert resolve_send_reasoning("acme", m) is False

    def test_no_cache_defaults_false(self, env, provider):
        """缓存缺失时默认不回传（避免向不支持的模型发非标准字段）。"""
        from mycode.providers import resolve_send_reasoning
        assert resolve_send_reasoning("acme", "m-r1") is False

    def test_explicit_overrides_default(self, env, provider):
        """显式配置优先于 models.dev 推导（两个方向都覆盖）。"""
        from mycode.providers import (ModelConfig, resolve_send_reasoning,
                                      save_model_config)
        self._write_cache(env, self._api())
        save_model_config("acme", ModelConfig(id="m-r1", send_reasoning_content=False))
        save_model_config("acme", ModelConfig(id="m-none", send_reasoning_content=True))
        assert resolve_send_reasoning("acme", "m-r1") is False
        assert resolve_send_reasoning("acme", "m-none") is True

    def test_resolve_extra_body(self, provider):
        """extra_body 解析：已配置返回 dict，未配置返回 None。"""
        from mycode.providers import (ModelConfig, resolve_extra_body,
                                      save_model_config)
        assert resolve_extra_body("acme", "m-r1") is None
        save_model_config("acme", ModelConfig(id="m-r1", extra_body={"x": 1}))
        assert resolve_extra_body("acme", "m-r1") == {"x": 1}

    def test_provider_level_flag_fallback(self, env, provider):
        """模型级未配置时回退到提供商级开关。"""
        from mycode.providers import (ProviderConfig, resolve_send_reasoning,
                                      save_provider)
        self._write_cache(env, self._api())
        save_provider(ProviderConfig(id="acme", name="Acme", base_url="u",
                                     api_key="k",
                                     models=["m-r1", "m-none"],
                                     send_reasoning_content=False))
        # 模型库推导为 True 的 m-r1 也被提供商级 False 覆盖
        assert resolve_send_reasoning("acme", "m-r1") is False
        assert resolve_send_reasoning("acme", "m-none") is False

    def test_provider_level_flag_true_for_non_interleaved(self, env, provider):
        """提供商级 True 可为不交错思考内容的模型开启回传。"""
        from mycode.providers import (ProviderConfig, resolve_send_reasoning,
                                      save_provider)
        self._write_cache(env, self._api())
        save_provider(ProviderConfig(id="acme", name="Acme", base_url="u",
                                     api_key="k", models=["m-none"],
                                     send_reasoning_content=True))
        assert resolve_send_reasoning("acme", "m-none") is True

    def test_model_level_overrides_provider_level(self, env, provider):
        """模型级配置优先于提供商级（两个方向）。"""
        from mycode.providers import (ModelConfig, ProviderConfig,
                                      resolve_send_reasoning, save_model_config,
                                      save_provider)
        self._write_cache(env, self._api())
        save_provider(ProviderConfig(id="acme", name="Acme", base_url="u",
                                     api_key="k", models=["m-r1", "m-none"],
                                     send_reasoning_content=False))
        save_model_config("acme", ModelConfig(id="m-r1",
                                              send_reasoning_content=True))
        assert resolve_send_reasoning("acme", "m-r1") is True
        assert resolve_send_reasoning("acme", "m-none") is False

    def test_extra_body_falls_back_to_provider(self, provider):
        """extra_body 同样逐级回退：模型级 → 提供商级。"""
        from mycode.providers import (ModelConfig, ProviderConfig,
                                      resolve_extra_body, save_model_config,
                                      save_provider)
        save_provider(ProviderConfig(id="acme", name="Acme", base_url="u",
                                     api_key="k", models=["m-r1", "m-none"],
                                     extra_body={"pv": 1}))
        assert resolve_extra_body("acme", "m-r1") == {"pv": 1}
        save_model_config("acme", ModelConfig(id="m-r1", extra_body={"m": 2}))
        assert resolve_extra_body("acme", "m-r1") == {"m": 2}
        assert resolve_extra_body("acme", "m-none") == {"pv": 1}

    def test_provider_level_persisted(self, env, provider):
        """提供商级两项写入 TOML。"""
        from mycode import config
        from mycode.providers import (ProviderConfig, load_providers,
                                      save_provider)
        save_provider(ProviderConfig(id="acme", name="A", base_url="u",
                                     api_key="k", models=["m-r1"],
                                     send_reasoning_content=False,
                                     extra_body={"a": 1}))
        body = open(config.CONFIG_FILE, encoding="utf-8").read()
        assert "send_reasoning_content = false" in body
        assert "extra_body = " in body
        p = load_providers()["acme"]
        assert p.send_reasoning_content is False
        assert p.extra_body == {"a": 1}

    def test_provider_level_absent_defaults_none(self, provider):
        """未配置的提供商级两项为 None（走后续回退）。"""
        from mycode.providers import load_providers
        p = load_providers()[provider]
        assert p.send_reasoning_content is None
        assert p.extra_body is None

    def test_save_provider_preserves_provider_level(self, env, provider):
        """再次保存（值未变）不会丢掉提供商级配置。"""
        from mycode.providers import (ModelConfig, ProviderConfig, load_providers,
                                      save_model_config, save_provider)
        save_provider(ProviderConfig(id=provider, name="P", base_url="u",
                                     api_key="k", models=["m1"],
                                     send_reasoning_content=True,
                                     extra_body={"a": 1}))
        save_model_config(provider, ModelConfig(id="m1", name="x"))
        save_provider(ProviderConfig(id=provider, name="P2", base_url="u",
                                     api_key="k", models=["m1"],
                                     send_reasoning_content=True,
                                     extra_body={"a": 1}))
        p = load_providers()[provider]
        assert p.name == "P2"
        assert p.send_reasoning_content is True
        assert p.extra_body == {"a": 1}

    def test_provider_presets_fallback(self, env, provider, default_pid):
        """提供商级未配置时回退到 provider_presets 预设值表。"""
        from mycode import provider_presets, providers as pvs
        assert provider_presets.lookup(
            default_pid).send_reasoning_content is True
        pvs.save_provider(pv.ProviderConfig(id=default_pid, name="D", base_url="u",
                                            api_key="k", models=["m1"]))
        assert pvs.resolve_send_reasoning(default_pid, "m1") is True
        assert pvs.resolve_extra_body(default_pid, "m1") == {
            "thinking": {"type": "enabled"}}

    def test_provider_presets_overridden_by_provider_and_model(self, env,
                                                                 default_pid):
        """提供商级 / 模型级配置都能覆盖预设值表。"""
        from mycode import providers as pvs
        pvs.save_provider(pv.ProviderConfig(
            id=default_pid, name="D", base_url="u", api_key="k", models=["m1"],
            send_reasoning_content=False, extra_body={"user": 1}))
        assert pvs.resolve_send_reasoning(default_pid, "m1") is False
        assert pvs.resolve_extra_body(default_pid, "m1") == {"user": 1}
        pvs.save_model_config(default_pid, pv.ModelConfig(
            id="m1", send_reasoning_content=True))
        assert pvs.resolve_send_reasoning(default_pid, "m1") is True

    def test_resolve_extra_body_returns_copy(self, env, default_pid):
        """resolve_extra_body 返回副本，改动不影响预设值表。"""
        from mycode import provider_presets
        from mycode import providers as pvs
        pvs.save_provider(pv.ProviderConfig(id=default_pid, name="D",
                                            base_url="u", api_key="k",
                                            models=["m1"]))
        got = pvs.resolve_extra_body(default_pid, "m1")
        got["thinking"]["type"] = "disabled"          # type: ignore[index]
        again = pvs.resolve_extra_body(default_pid, "m1")
        assert again["thinking"]["type"] == "enabled"  # type: ignore[index]
        assert provider_presets.lookup(
            default_pid).extra_body["thinking"][       # type: ignore[index]
            "type"] == "enabled"

    def test_provider_presets_not_applied_to_other_providers(self, env,
                                                               provider):
        """未命中 for_providers 的提供商不受预设影响。"""
        from mycode import providers as pvs
        self._write_cache(env, self._api())
        pvs.save_provider(pv.ProviderConfig(id="other", name="X", base_url="u",
                                           api_key="k", models=["m-none"]))
        assert pvs.resolve_send_reasoning("other", "m-none") is False
        assert pvs.resolve_extra_body("other", "m-none") is None

    def test_provider_presets_beats_models_dev(self, env, default_pid):
        """预设值表优先于 models.dev 的 interleaved 推导。"""
        from mycode import provider_presets
        from mycode import providers as pvs
        self._write_cache(env, {default_pid: {
            "npm": "@ai-sdk/openai-compatible", "name": "D", "api": "u",
            "models": {"m1": {"id": "m1", "name": "M1"}}}})
        pvs.save_provider(pv.ProviderConfig(id=default_pid, name="D",
                                            base_url="u", api_key="k",
                                            models=["m1"]))
        # 表里开关为 True，而模型库缓存推导为 False
        assert provider_presets.lookup(
            default_pid).send_reasoning_content is True
        assert pvs.resolve_send_reasoning(default_pid, "m1") is True

    def test_resolve_model_name(self, env, provider):
        """显示名：配置 > models.dev 缓存 > 模型 id。"""
        from mycode.providers import ModelConfig, resolve_model_name, save_model_config
        self._write_cache(env, self._api())
        assert resolve_model_name("acme", "m-r1") == "R1"
        save_model_config("acme", ModelConfig(id="m-r1", name="自定义"))
        assert resolve_model_name("acme", "m-r1") == "自定义"
        # 缓存里没有的模型回退 id
        assert resolve_model_name("acme", "unknown-model") == "unknown-model"
