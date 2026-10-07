"""
模型数据源注册表（mycode.models_registry）测试。

覆盖：

- ``metadata`` 读写（meta.json：updated_at / status / etag /
  providers_count / error）
- ``is_fresh`` 判定（同一天的 success / not_modified 才算新）
- ``load_cached_api``：缓存不存在返回 None、存在返回 dict、解析失败返回 None
- ``update_cache``：
  - 缓存不存在时全量下载（不带 If-None-Match）并原子写入
  - 缓存存在且非当天时带 If-None-Match，304 只更新 meta 不改缓存
  - 下载/解析失败写 meta 的 error 状态
- 候选提供商解析：仅 ``npm == "@ai-sdk/openai-compatible"`` 入选；
  model 的 ``tool_call == false`` 被过滤（缺字段视为支持）
- ``_replace_url_path``：只替换 URL 的 path 段，其余部分保留
- 预设 ``openai_compatible`` 的提供商（npm 标注其他包）也纳入候选，
  ``api`` 的 URL path 被替换为预设的 ``path``
- ``resolve_base_url``：``${VAR}`` 插值渲染（缺失变量替换为空串）
- ``is_secret_env_var``：名称含 KEY / TOKEN / PAT 视为密钥
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mycode import models_registry as mr


# ===================================================================
# 路径与常量
# ===================================================================

def _cache_dir() -> Path:
    return Path(os.environ["MYCODE_HOME_DIR"]) / "models_cache"


def _write_cache(data: dict) -> Path:
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "api.json"
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    return cache_file


def _write_meta(meta: dict) -> Path:
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    meta_file = cache_dir / "meta.json"
    meta_file.write_text(json.dumps(meta), encoding="utf-8")
    return meta_file


# ===================================================================
# 元数据读写与新鲜度
# ===================================================================

class TestMetadata:
    def test_roundtrip(self):
        meta = {
            "updated_at": "2026-09-26T01:00:00+08:00",
            "status": "success",
            "etag": '"abc"',
            "providers_count": 182,
        }
        _write_meta(meta)
        assert mr.load_meta() == meta

    def test_missing_returns_empty(self):
        assert mr.load_meta() == {}

    def test_invalid_json_returns_empty(self):
        meta_file = _cache_dir() / "meta.json"
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        meta_file.write_text("{bad json", encoding="utf-8")
        assert mr.load_meta() == {}

    def test_save_meta_writes_file(self):
        meta = {"updated_at": "2026-09-26T01:00:00+08:00", "status": "success"}
        mr.save_meta(meta)
        assert json.loads(_cache_dir().joinpath("meta.json").read_text()) == meta


class TestIsFresh:
    @pytest.fixture(autouse=True)
    def _clean(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MYCODE_HOME_DIR", str(tmp_path / ".mycode"))
        yield

    def test_success_today_is_fresh(self):
        now = "2026-09-26T12:00:00+08:00"
        assert mr.is_fresh({"updated_at": now, "status": "success"}, now) is True

    def test_not_modified_today_is_fresh(self):
        now = "2026-09-26T12:00:00+08:00"
        assert mr.is_fresh({"updated_at": now, "status": "not_modified"}, now) is True

    def test_yesterday_is_stale(self):
        now = "2026-09-26T12:00:00+08:00"
        meta = {"updated_at": "2026-09-25T23:59:59+08:00", "status": "success"}
        assert mr.is_fresh(meta, now) is False

    def test_error_today_is_stale(self):
        now = "2026-09-26T12:00:00+08:00"
        meta = {"updated_at": now, "status": "error", "error": "boom"}
        assert mr.is_fresh(meta, now) is False

    def test_missing_field_is_stale(self):
        now = "2026-09-26T12:00:00+08:00"
        assert mr.is_fresh({}, now) is False


# ===================================================================
# 缓存加载
# ===================================================================

class TestLoadCachedApi:
    def test_missing_returns_none(self):
        assert mr.load_cached_api() is None

    def test_valid_returns_dict(self):
        _write_cache({"a": 1})
        assert mr.load_cached_api() == {"a": 1}

    def test_invalid_json_returns_none(self):
        cache_file = _cache_dir() / "api.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text("{bad json", encoding="utf-8")
        assert mr.load_cached_api() is None


# ===================================================================
# update_cache（mock httpx）
# ===================================================================

class _FakeResponse:
    def __init__(self, status_code=200, content=b"{}", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers if headers is not None else {}


class TestUpdateCache:
    @pytest.fixture(autouse=True)
    def _clean(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MYCODE_HOME_DIR", str(tmp_path / ".mycode"))
        yield

    def test_cold_full_download_no_etag_header(self, monkeypatch):
        calls = []

        def fake_get(url, headers=None, timeout=None, **kwargs):
            calls.append({"url": url, "headers": headers, "timeout": timeout})
            return _FakeResponse(
                200,
                json.dumps({"p": {"npm": "@ai-sdk/openai-compatible"}}).encode(),
                {"etag": '"cold"'},
            )

        monkeypatch.setattr(mr.httpx, "get", fake_get)
        now = "2026-09-26T12:00:00+08:00"
        meta = mr.update_cache(timeout=30, now=now)

        assert len(calls) == 1
        assert calls[0]["url"] == mr.API_URL
        assert calls[0]["headers"] == {}  # 无缓存 → 不带 If-None-Match
        assert calls[0]["timeout"] == 30
        # 缓存写入
        assert json.loads(_cache_dir().joinpath("api.json").read_text()) == {
            "p": {"npm": "@ai-sdk/openai-compatible"}
        }
        # meta 更新
        assert meta["status"] == "success"
        assert meta["etag"] == '"cold"'
        assert meta["updated_at"] == now

    def test_etag_header_when_stale(self, monkeypatch):
        _write_cache({"old": 1})
        _write_meta({
            "updated_at": "2026-09-25T01:00:00+08:00",
            "status": "success",
            "etag": '"old-etag"',
        })
        calls = []

        def fake_get(url, headers=None, timeout=None, **kwargs):
            calls.append(headers)
            return _FakeResponse(304)

        monkeypatch.setattr(mr.httpx, "get", fake_get)
        now = "2026-09-26T12:00:00+08:00"
        meta = mr.update_cache(timeout=30, now=now)

        assert calls[0] == {"If-None-Match": '"old-etag"'}
        assert meta["status"] == "not_modified"
        assert meta["etag"] == '"old-etag"'
        assert meta["updated_at"] == now
        # 304 不改写缓存
        assert json.loads(_cache_dir().joinpath("api.json").read_text()) == {"old": 1}

    def test_fresh_skips_download(self, monkeypatch):
        _write_cache({"old": 1})
        _write_meta({
            "updated_at": "2026-09-26T01:00:00+08:00",
            "status": "success",
            "etag": '"x"',
        })
        called = []

        def fake_get(*a, **k):
            called.append(1)
            return _FakeResponse(200)

        monkeypatch.setattr(mr.httpx, "get", fake_get)
        now = "2026-09-26T12:00:00+08:00"
        meta = mr.update_cache(timeout=30, now=now)
        assert called == []
        assert meta["updated_at"] == "2026-09-26T01:00:00+08:00"

    def test_error_saves_meta_error(self, monkeypatch):
        def fake_get(*a, **k):
            raise RuntimeError("网络抖动")

        monkeypatch.setattr(mr.httpx, "get", fake_get)
        now = "2026-09-26T12:00:00+08:00"
        meta = mr.update_cache(timeout=30, now=now)
        assert meta["status"] == "error"
        assert "网络抖动" in meta["error"]
        assert meta["updated_at"] == now
        # 无缓存文件产生
        assert not _cache_dir().joinpath("api.json").exists()

    def test_invalid_json_saves_error(self, monkeypatch):
        monkeypatch.setattr(
            mr.httpx, "get",
            lambda *a, **k: _FakeResponse(200, b"{bad json"),
        )
        now = "2026-09-26T12:00:00+08:00"
        meta = mr.update_cache(timeout=30, now=now)
        assert meta["status"] == "error"
        assert not _cache_dir().joinpath("api.json").exists()

    def test_atomic_replace_existing(self, monkeypatch):
        _write_cache({"old": 1})
        _write_meta({
            "updated_at": "2026-09-25T01:00:00+08:00",
            "status": "success",
            "etag": '"old"',
        })

        monkeypatch.setattr(
            mr.httpx, "get",
            lambda *a, **k: _FakeResponse(200, b'{"new": 2}', {"etag": '"new"'}),
        )
        now = "2026-09-26T12:00:00+08:00"
        meta = mr.update_cache(timeout=30, now=now)
        assert json.loads(_cache_dir().joinpath("api.json").read_text()) == {"new": 2}
        assert meta["etag"] == '"new"'

    def test_keep_old_etag_when_response_missing(self, monkeypatch):
        _write_cache({"old": 1})
        _write_meta({
            "updated_at": "2026-09-25T01:00:00+08:00",
            "status": "success",
            "etag": '"old"',
        })
        monkeypatch.setattr(
            mr.httpx, "get",
            lambda *a, **k: _FakeResponse(200, b'{"new": 2}'),
        )
        now = "2026-09-26T12:00:00+08:00"
        meta = mr.update_cache(timeout=30, now=now)
        assert meta["etag"] == '"old"'

    def test_start_async_update_returns_daemon_thread(self, monkeypatch):
        made = []

        def fake_update(*a, **k):
            made.append(1)

        monkeypatch.setattr(mr, "update_cache", fake_update)
        th = mr.start_async_update(timeout=1)
        assert th.daemon is True
        th.join(timeout=1)
        assert made == [1]


# ===================================================================
# 候选解析
# ===================================================================

AVAILABLE_MODEL = {
    "id": "m-good",
    "tool_call": True,
}
NO_TOOL_MODEL = {
    "id": "m-no-tool",
    "tool_call": False,
}
NO_TOOL_FIELD_MODEL = {
    "id": "m-tool-missing",
}


def _models_dict(models: list[dict]) -> dict:
    """真实 api.json 的 models 是 dict（id → 模型对象）。"""
    return {m["id"]: m for m in models}


def _api_data() -> dict:
    return {
        "openai-compat-1": {
            "id": "openai-compat-1",
            "npm": "@ai-sdk/openai-compatible",
            "name": "Compat One",
            "env": ["COMPAT_ONE_API_KEY"],
            "api": "https://api.example.com/v1",
            "models": _models_dict(
                [AVAILABLE_MODEL, NO_TOOL_MODEL, NO_TOOL_FIELD_MODEL]
            ),
        },
        "openai-compat-2": {
            "id": "openai-compat-2",
            "npm": "@ai-sdk/openai-compatible",
            "name": "Compat Two",
            "env": ["COMPAT_TWO_API_KEY"],
            "api": "https://${HOST}/v1",
            "models": _models_dict([AVAILABLE_MODEL]),
        },
        "openai-native": {
            "id": "openai-native",
            "npm": "@ai-sdk/openai",
            "name": "Not Compatible",
            "env": ["OPENAI_API_KEY"],
            "api": None,
            "models": _models_dict([AVAILABLE_MODEL]),
        },
    }


class TestCandidateProviders:
    def test_only_openai_compatible_included(self):
        providers = mr.candidate_providers(_api_data())
        assert set(providers) == {"openai-compat-1", "openai-compat-2"}

    def test_fields_extracted(self):
        providers = mr.candidate_providers(_api_data())
        p = providers["openai-compat-1"]
        assert p.id == "openai-compat-1"
        assert p.name == "Compat One"
        assert p.base_url == "https://api.example.com/v1"
        assert p.env == ["COMPAT_ONE_API_KEY"]
        assert p.models == {"m-good": mr.ModelInfo(id="m-good"),
                            "m-tool-missing": mr.ModelInfo(id="m-tool-missing")}

    def test_tool_call_false_filtered(self):
        providers = mr.candidate_providers(_api_data())
        assert "m-no-tool" not in providers["openai-compat-1"].models
        # 缺 tool_call 字段视为支持
        assert "m-tool-missing" in providers["openai-compat-1"].models

    def test_models_sorted_by_api_order(self):
        providers = mr.candidate_providers(_api_data())
        assert list(providers["openai-compat-1"].models) == [
            "m-good", "m-tool-missing"]

    def test_model_names_extracted(self):
        """models 为 id → ModelInfo 映射；缺 name 字段时 name 为空串。"""
        data = {
            "p": {
                "id": "p", "npm": "@ai-sdk/openai-compatible", "name": "P",
                "models": _models_dict([
                    {"id": "m1", "name": "M One"},
                    {"id": "m2"},
                ]),
            },
        }
        providers = mr.candidate_providers(data)
        assert providers["p"].models == {
            "m1": mr.ModelInfo(id="m1", name="M One"),
            "m2": mr.ModelInfo(id="m2"),
        }


# ===================================================================
# 预设 openai_compatible（models.dev 标注其他 npm 但提供 OpenAI 兼容 API）
# ===================================================================

class TestPresetOpenAICompatible:
    """npm 非 openai-compatible 时，查 provider_presets 的 openai_compatible：
    命中则把 ``api`` 的 URL path 替换为预设的 ``path`` 后纳入候选。"""

    @staticmethod
    def _install_preset(monkeypatch, pids: list[str], path: str = "/v1"):
        from mycode import provider_presets as pp

        t = [pp.ProviderPreset(
            id="test-oai", name="测试预设",
            for_providers=tuple(pids),
            openai_compatible=pp.OpenAICompatible(path=path))]
        monkeypatch.setattr(pp, "_PROVIDER_PRESETS", t)
        monkeypatch.setattr(pp, "_OAI_BY_PROVIDER", pp._rebuild_oai_index(t))
        return t

    def test_preset_provider_included_with_path_replaced(self, monkeypatch):
        """命中预设：api 的 URL path 被替换，scheme / netloc 保留。"""
        self._install_preset(monkeypatch, ["anthropic-style"])
        data = {
            "anthropic-style": {
                "npm": "@ai-sdk/anthropic",
                "name": "Anthropic Style",
                "api": "https://api.example.com/anthropic/v1",
                "models": _models_dict([AVAILABLE_MODEL]),
            },
        }
        providers = mr.candidate_providers(data)
        assert set(providers) == {"anthropic-style"}
        p = providers["anthropic-style"]
        assert p.base_url == "https://api.example.com/v1"
        assert p.models == {"m-good": mr.ModelInfo(id="m-good")}

    def test_presets_different_paths(self, monkeypatch):
        """预设 path 可为任意 path 段（如多级路径）。"""
        self._install_preset(monkeypatch, ["multi-path"], path="/openai/v1")
        data = {
            "multi-path": {
                "npm": "@ai-sdk/anthropic",
                "api": "https://api.example.com/anthropic/v1",
                "models": _models_dict([AVAILABLE_MODEL]),
            },
        }
        assert mr.candidate_providers(
            data)["multi-path"].base_url == "https://api.example.com/openai/v1"

    def test_no_preset_still_excluded(self):
        """未命中预设：仍按 npm 过滤掉。"""
        data = {
            "anthropic-style": {
                "npm": "@ai-sdk/anthropic",
                "api": "https://api.example.com/anthropic/v1",
                "models": _models_dict([AVAILABLE_MODEL]),
            },
        }
        assert mr.candidate_providers(data) == {}

    def test_preset_but_api_missing_excluded(self, monkeypatch):
        """命中预设但 raw 无 api 模板：无从替换 path，不入选。"""
        self._install_preset(monkeypatch, ["no-api"])
        data = {
            "no-api": {
                "npm": "@ai-sdk/anthropic",
                "api": None,
                "models": _models_dict([AVAILABLE_MODEL]),
            },
        }
        assert mr.candidate_providers(data) == {}

    def test_native_openai_compatible_unchanged_by_preset(self, monkeypatch):
        """npm 本就是 openai-compatible 时不查预设，api 原样保留。"""
        self._install_preset(monkeypatch, ["openai-compat-1"], path="/v9")
        data = _api_data()
        providers = mr.candidate_providers(data)
        assert providers["openai-compat-1"].base_url == (
            "https://api.example.com/v1")


# ===================================================================
# _replace_url_path
# ===================================================================

class TestReplaceUrlPath:
    def test_replace_path_keep_host(self):
        assert mr._replace_url_path(
            "https://api.example.com/anthropic/v1", "/v1") == (
            "https://api.example.com/v1")

    def test_multi_segment_path(self):
        assert mr._replace_url_path(
            "https://api.example.com/old/path", "/openai/v1") == (
            "https://api.example.com/openai/v1")

    def test_query_and_fragment_kept(self):
        assert mr._replace_url_path(
            "https://api.example.com/anthropic/v1?x=1#frag", "/v1") == (
            "https://api.example.com/v1?x=1#frag")

    def test_empty_path_becomes_root(self):
        assert mr._replace_url_path("https://api.example.com/anthropic", "") == (
            "https://api.example.com")


# ===================================================================
# base_url 插值 & 密钥变量判断
# ===================================================================

class TestResolveBaseUrl:
    def test_substitute_var(self):
        tmpl = "https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/ai/v1"
        assert mr.resolve_base_url(tmpl, {"ACCOUNT_ID": "abc"}) == (
            "https://api.cloudflare.com/client/v4/accounts/abc/ai/v1"
        )

    def test_missing_var_empty(self):
        tmpl = "https://${HOST}/v1"
        assert mr.resolve_base_url(tmpl, {}) == "https:///v1"

    def test_plain_url_unchanged(self):
        assert mr.resolve_base_url("https://api.example.com/v1", {}) == (
            "https://api.example.com/v1"
        )


class TestIsSecretEnvVar:
    def test_key_token_pat(self):
        assert mr.is_secret_env_var("DEEPSEEK_API_KEY") is True
        assert mr.is_secret_env_var("GITHUB_TOKEN") is True
        assert mr.is_secret_env_var("CLARIFAI_PAT") is True

    def test_other_false(self):
        assert mr.is_secret_env_var("DATABRICKS_HOST") is False
        assert mr.is_secret_env_var("CLOUDFLARE_ACCOUNT_ID") is False
        assert mr.is_secret_env_var("NEON_AI_GATEWAY_BASE_URL") is False

# ===================================================================
# interleaved → interleaves_reasoning（回传 reasoning_content 的默认值来源）
# ===================================================================

class TestInterleavesReasoning:
    """``interleaved`` 字段只认 ``{"field": "reasoning_content"}`` 一种形态。"""

    @pytest.mark.parametrize("interleaved", [
        {"field": "reasoning_content"},
        pytest.param({"field": "reasoning_content", "extra": 1}, id="带多余键"),
    ])
    def test_field_reasoning_content_true(self, interleaved):
        assert mr.interleaves_reasoning({"interleaved": interleaved}) is True

    @pytest.mark.parametrize("interleaved", [
        True,
        False,
        {"field": "reasoning_details"},
        {"field": "thinking"},
        {"field": None},
        {},
        "reasoning_content",
        None,
    ])
    def test_other_forms_false(self, interleaved):
        assert mr.interleaves_reasoning({"interleaved": interleaved}) is False

    def test_missing_or_non_dict(self):
        assert mr.interleaves_reasoning({}) is False
        assert mr.interleaves_reasoning({"id": "m"}) is False
        assert mr.interleaves_reasoning(None) is False
        assert mr.interleaves_reasoning("x") is False

    def test_candidate_providers_populates_flag(self):
        """candidate_providers 把 interleaved 解析结果写进 ModelInfo。"""
        api = {"acme": {"npm": "@ai-sdk/openai-compatible", "name": "Acme",
                        "api": "https://a/v1", "models": {
            "m-r1": {"id": "m-r1", "name": "R1",
                     "interleaved": {"field": "reasoning_content"}},
            "m-plain": {"id": "m-plain", "name": "P"},
        }}}
        models = mr.candidate_providers(api)["acme"].models
        assert models["m-r1"].interleaves_reasoning is True
        assert models["m-plain"].interleaves_reasoning is False

    def test_model_info_default_false(self):
        assert mr.ModelInfo(id="m").interleaves_reasoning is False
