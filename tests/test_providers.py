"""
供应商配置模块（mycode.providers）测试。

覆盖：

- ``ProviderConfig`` 数据
- ``save_provider`` / ``load_providers`` / ``delete_provider``：保注释读写、
  更新已有 section、删除
- ``set_current`` / ``get_current`` / ``is_configured``
- ``next_user_defined_id``：从小到大取未占用的 user-defined-N
- ``migrate_legacy``：
  - 顶层 api_key/base_url（任一非空）迁移为 [providers.user-defined-N]，
    name 取 base_url host，models 取旧 model_name
  - 保留 model_name 并补写 model_provider；删除顶层 api_key/base_url
  - 环境变量 MYCODE_API_KEY 等不参与迁移
  - 无旧配置时返回 None
  - 已有 user-defined-N 时取未占用下一个
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
models = ["deepseek-chat"]
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
        assert 'model_name = "deepseek-reasoner"' in text

    def test_current_persists_comment(self):
        _write_config(BASE_DOC)
        pv.set_current("deepseek", "deepseek-chat")
        assert "# 顶部注释" in _read_config()

    def test_is_configured(self):
        assert pv.is_configured() is False
        pv.save_provider(_provider())
        assert pv.is_configured() is True


# ===================================================================
# user-defined id 分配
# ===================================================================

class TestNextUserDefined:
    def test_first_is_1(self):
        _write_config("")
        assert pv.next_user_defined_id() == "user-defined-1"

    def test_skips_existing(self):
        _write_config(
            '[providers.user-defined-1]\nname="a"\n\n'
            '[providers.user-defined-3]\nname="b"\n'
        )
        assert pv.next_user_defined_id() == "user-defined-2"


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
        assert pid == "user-defined-1"
        ps = pv.load_providers()
        assert set(ps) == {"user-defined-1"}
        p = ps["user-defined-1"]
        assert p.name == "api.openai.com"
        assert p.base_url == "https://api.openai.com/v1"
        assert p.api_key == "legacy-key"
        assert p.models == ["gpt-4o"]
        assert pv.get_current() == ("user-defined-1", "gpt-4o")
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
        assert pid == "user-defined-1"
        p = pv.load_providers()["user-defined-1"]
        assert p.base_url == "http://127.0.0.1:1234/v1"
        assert p.api_key == ""
        assert p.models == []

    def test_ignores_env_vars(self, monkeypatch):
        _write_config('base_url = "https://api.openai.com/v1"\n')
        monkeypatch.setenv("MYCODE_API_KEY", "from-env")
        monkeypatch.setenv("MYCODE_BASE_URL", "https://env.example/v1")
        monkeypatch.setenv("MYCODE_MODEL_NAME", "env-model")
        pid = pv.migrate_legacy()
        assert pid == "user-defined-1"
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
            '[providers.user-defined-1]\n'
            'name = "existing"\n'
        )
        pid = pv.migrate_legacy()
        assert pid == "user-defined-2"
        assert set(pv.load_providers()) == {"user-defined-1", "user-defined-2"}


# ===================================================================
# 写回后 config 缓存失效
# ===================================================================

class TestConfigInvalidation:
    def test_set_current_seen_by_config_get(self):
        _write_config(BASE_DOC)
        pv.set_current("deepseek", "deepseek-reasoner")
        assert config.get("model_provider") == "deepseek"
        assert config.get("model_name") == "deepseek-reasoner"

    def test_set_current_seen_by_config_get_after_second_write(self):
        _write_config(BASE_DOC)
        pv.set_current("deepseek", "a")
        config.get("model_name")  # 触发一次缓存
        pv.set_current("deepseek", "b")
        assert config.get("model_name") == "b"