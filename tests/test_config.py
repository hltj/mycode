"""
config 模块测试：环境变量 > config.toml > 默认值的优先级，
以及各类型读取函数（str / int / list / int_list）的解析行为。
"""

from __future__ import annotations

import os

import pytest

from mycode import config


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch, tmp_path):
    """每个测试指向独立的 HOME 目录并清掉 config 缓存。

    同时清掉 conftest.fake_env 设置的 MYCODE_* 环境变量，
    避免干扰优先级断言（测试内按需再设置）。
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MYCODE_HOME_DIR", str(home))
    for name in list(os.environ):
        if name.startswith("MYCODE_") and name != "MYCODE_HOME_DIR":
            monkeypatch.delenv(name, raising=False)
    config._raw_cache = None
    config._raw_loaded = False
    # CONFIG_FILE 是导入时确定的常量，测试里用 monkeypatch 替换
    monkeypatch.setattr(config, "CONFIG_FILE", str(home / "config.toml"))
    yield
    config._raw_cache = None
    config._raw_loaded = False


def _write_config(text: str, path=None):
    import pathlib
    p = pathlib.Path(path if path else config.CONFIG_FILE)
    p.write_text(text, encoding="utf-8")


class TestPriority:
    """优先级：环境变量 > config.toml > 默认值。"""

    def test_env_overrides_toml(self, monkeypatch):
        _write_config("api_key = \"from-toml\"\n")
        monkeypatch.setenv("MYCODE_API_KEY", "from-env")
        assert config.get("api_key") == "from-env"

    def test_toml_over_default(self):
        _write_config("api_key = \"from-toml\"\n")
        assert config.get("api_key") == "from-toml"

    def test_default_when_unset(self):
        assert config.get("api_key") is None
        assert config.get("model_name", "gpt-4o") == "gpt-4o"

    def test_empty_env_falls_back_to_toml(self, monkeypatch):
        # 环境变量设为空字符串视为未设置，回退 TOML
        _write_config("model_name = \"toml-model\"\n")
        monkeypatch.setenv("MYCODE_MODEL_NAME", "")
        assert config.get("model_name") == "toml-model"

    def test_empty_toml_falls_back_to_default(self):
        _write_config("model_name = \"\"\n")
        assert config.get("model_name", "fallback") == "fallback"

    def test_uppercase_toml_key_ignored(self):
        # TOML 键只认小写，大写风格不识别（回退默认值）
        _write_config("MODEL_NAME = \"upper-model\"\n")
        assert config.get("model_name") is None

    def test_missing_file_ignored(self):
        # config.toml 不存在：直接用环境变量 / 默认值
        assert config.get("api_key") is None

    def test_broken_toml_ignored(self, capsys):
        _write_config("this is not = = valid toml [[[\n")
        assert config.get("api_key") is None
        assert "解析失败" in capsys.readouterr().err


class TestGetStr:
    def test_toml_non_str_coerced(self):
        _write_config("bash_timeout = 30\n")
        assert config.get("bash_timeout") == "30"


class TestGetInt:
    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("MYCODE_BASH_TIMEOUT", "15")
        assert config.get_int("bash_timeout", 60) == 15

    def test_from_toml(self):
        _write_config("bash_timeout = 25\n")
        assert config.get_int("bash_timeout", 60) == 25

    def test_default_on_invalid(self, monkeypatch):
        monkeypatch.setenv("MYCODE_BASH_TIMEOUT", "abc")
        assert config.get_int("bash_timeout", 60) == 60

    def test_default_when_unset(self):
        assert config.get_int("bash_timeout", 60) == 60


class TestGetList:
    def test_env_comma_separated(self, monkeypatch):
        monkeypatch.setenv("MYCODE_BASH_DANGEROUS", "sudo, rm -rf ,")
        assert config.get_list("bash_dangerous") == ["sudo", "rm -rf"]

    def test_toml_array(self):
        _write_config("bash_dangerous = [\"sudo\", \"rm -rf\"]\n")
        assert config.get_list("bash_dangerous") == ["sudo", "rm -rf"]

    def test_toml_string_not_split(self):
        # TOML 中列表项必须是数组；字符串不按逗号切分，整体作为单项
        _write_config("bash_caution = \"rm, ^git\"\n")
        assert config.get_list("bash_caution") == ["rm, ^git"]

    def test_unset_returns_none(self):
        assert config.get_list("bash_caution") is None


class TestGetIntList:
    def test_env_valid(self, monkeypatch):
        monkeypatch.setenv("MYCODE_E429_WAIT_SECONDS", "1,2,5,10")
        assert config.get_int_list("e429_wait_seconds") == [1, 2, 5, 10]

    def test_env_spaces(self, monkeypatch):
        monkeypatch.setenv("MYCODE_E429_WAIT_SECONDS", "1, 2 ,3")
        assert config.get_int_list("e429_wait_seconds") == [1, 2, 3]

    def test_toml_array(self):
        _write_config("e429_wait_seconds = [1, 2, 5, 10]\n")
        assert config.get_int_list("e429_wait_seconds") == [1, 2, 5, 10]

    def test_unset_returns_none(self):
        assert config.get_int_list("e429_wait_seconds") is None

    def test_toml_string_not_accepted(self):
        # TOML 中必须是数组；逗号分隔字符串不解析
        _write_config("e429_wait_seconds = \"1,2,5\"\n")
        assert config.get_int_list("e429_wait_seconds") is None

    @pytest.mark.parametrize("bad", ["abc,2", "1,,", "1,0", "1,-3", "1.5,2", ",1"])
    def test_env_invalid_returns_none(self, monkeypatch, bad):
        monkeypatch.setenv("MYCODE_E429_WAIT_SECONDS", bad)
        assert config.get_int_list("e429_wait_seconds") is None

    def test_toml_array_invalid_returns_none(self):
        _write_config("e429_wait_seconds = [1, 0, 3]\n")
        assert config.get_int_list("e429_wait_seconds") is None
