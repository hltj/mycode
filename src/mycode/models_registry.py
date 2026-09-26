"""
模型数据源注册表模块。

从 models.dev 的 ``api.json?type=all`` 拉取 / 缓存供应商与模型数据，并解析
出「OpenAI 兼容」候选供应商（``"npm": "@ai-sdk/openai-compatible"``），供
``/provider`` 供应商配置与 ``/model`` 模型切换界面使用。

设计概要：

- 缓存目录：``{MYCODE_HOME_DIR}/models_cache/``；
  - ``api.json``：接口原文缓存；
  - ``meta.json``：更新状态（updated_at / status / etag /
    providers_count / error）。
- 新鲜度：``status`` 为 success / not_modified 且 ``updated_at`` 与今天
  同一天视为新鲜，跳过下载。
- 更新策略（调用方用后台线程执行 ``start_async_update``）：
  - 缓存不存在：全量下载（不带 ``If-None-Match``）；
  - 缓存存在且陈旧：带 ``If-None-Match: <etag>`` 请求，304 只更新时间戳
    与 ``status=not_modified``，200 原子替换缓存；
  - 任何失败只写 meta 的 ``status=error``。
- 下载用 ``httpx``（``http://`` 与 ``https://``；压缩 zstd/brotli/gzip 走
  httpx 默认协商，安装 ``httpx[zstd]`` 后优先 zstd）。
- 候选解析：只保留 npm 为 ``@ai-sdk/openai-compatible`` 的供应商；
  每个 model 缺 ``tool_call`` 或为 true 才保留（agent 必须工具调用）。
- 供应商 ``api`` 字段是 `${VAR}` 模板，``resolve_base_url`` 负责插值渲染；
  ``is_secret_env_var`` 识别 env 列表里的密钥类变量（含 KEY/TOKEN/PAT）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

API_URL = "https://models.dev/api.json?type=all"


def _home_root() -> Path:
    return Path(
        os.path.expanduser(
            os.getenv("MYCODE_HOME_DIR", os.path.expanduser("~/.mycode"))
        )
    )


def _cache_dir() -> Path:
    return _home_root() / "models_cache"


def _cache_file() -> Path:
    return _cache_dir() / "api.json"


def _meta_file() -> Path:
    return _cache_dir() / "meta.json"


@dataclass
class ProviderInfo:
    """候选供应商的规范化视图。"""

    id: str
    name: str
    base_url: str
    env: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 元数据与缓存读写
# ---------------------------------------------------------------------------

def _iso_day(iso: str) -> str:
    """取 ISO 时间串的日期部分（YYYY-MM-DD），失败返回原串。"""
    try:
        return iso[:10]
    except Exception:
        return iso


def load_meta() -> dict:
    """读取 meta.json；不存在或解析失败返回空 dict。"""
    try:
        with open(_meta_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_meta(meta: dict) -> None:
    """原子写入 meta.json。"""
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(tmp, _meta_file())
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_cached_api() -> dict | None:
    """读取缓存 api.json；不存在或解析失败返回 None。"""
    try:
        with open(_cache_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _write_cache(data: dict) -> None:
    """原子写入 api.json。"""
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _cache_file())
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def is_fresh(meta: dict, now_iso: str | None = None) -> bool:
    """meta 是否当天新鲜（success / not_modified 且 updated_at 同一日期）。"""
    if not isinstance(meta, dict):
        return False
    status = meta.get("status")
    if status not in ("success", "not_modified"):
        return False
    updated = meta.get("updated_at")
    if not isinstance(updated, str) or not updated:
        return False
    now = now_iso or datetime.now().astimezone().isoformat()
    return _iso_day(updated) == _iso_day(now)


def _now_iso(now: str | None) -> str:
    return now or datetime.now().astimezone().isoformat()


# ---------------------------------------------------------------------------
# 下载与更新
# ---------------------------------------------------------------------------

def _download(etag: str | None, timeout: int) -> tuple[bytes | None, dict]:
    """执行下载：返回 (内容或 None, meta 补充字段)。

    ``etag`` 非空时带 ``If-None-Match`` 头；304 返回 (None, status 等)。
    """
    headers = {"If-None-Match": etag} if etag else {}
    resp = httpx.get(API_URL, headers=headers, timeout=timeout)
    if resp.status_code == 304:
        return None, {"status": "not_modified", "etag": etag}
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"下载失败：HTTP {resp.status_code}")
    return resp.content, {
        "status": "success",
        "etag": (resp.headers.get("etag") or etag or ""),
    }


def update_cache(
    timeout: int = 30,
    now: str | None = None,
) -> dict:
    """执行一次缓存更新检查。

    新鲜时直接返回现有 meta（不下载）；否则下载并做 etag / 304 判定，
    成功后原子替换缓存、失败只写 error meta。返回最新 meta dict。
    """
    meta = load_meta()
    now_iso = _now_iso(now)
    if is_fresh(meta, now_iso):
        return meta

    old_etag = meta.get("etag") if isinstance(meta, dict) else None
    try:
        content, extra = _download(old_etag, timeout)
    except Exception as e:
        meta = {
            "updated_at": now_iso,
            "status": "error",
            "error": str(e),
        }
        if old_etag:
            meta["etag"] = old_etag
        save_meta(meta)
        return meta

    if content is None:
        # 304：沿用旧缓存，只更新 meta 时间与状态
        meta = dict(meta)
        meta["updated_at"] = now_iso
        meta["status"] = "not_modified"
        meta["etag"] = old_etag or meta.get("etag", "")
        save_meta(meta)
        return meta

    # 200：解析并原子替换缓存
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("接口返回不是 JSON 对象")
    except (json.JSONDecodeError, ValueError) as e:
        meta = {
            "updated_at": now_iso,
            "status": "error",
            "error": f"解析 api.json 失败: {e}",
        }
        if old_etag:
            meta["etag"] = old_etag
        save_meta(meta)
        return meta

    _write_cache(data)
    meta = {
        "updated_at": now_iso,
        "status": extra["status"],
        "etag": extra["etag"] or old_etag or "",
    }
    if not meta["etag"]:
        meta.pop("etag", None)
    save_meta(meta)
    return meta


def start_async_update(timeout: int = 30) -> threading.Thread:
    """后台 daemon 线程执行一次缓存更新（不阻塞、不抛出到调用方）。"""
    def _run() -> None:
        try:
            update_cache(timeout=timeout)
        except Exception:
            # 静默失败：更新失败已在 meta 中体现；不干扰前台。
            pass

    th = threading.Thread(target=_run, daemon=True, name="models-registry-update")
    th.start()
    return th


# ---------------------------------------------------------------------------
# 候选解析
# ---------------------------------------------------------------------------

_NPM_OPENAI_COMPATIBLE = "@ai-sdk/openai-compatible"
_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _model_id(obj: object) -> str | None:
    if not isinstance(obj, dict):
        return None
    raw = obj.get("id")
    return raw if isinstance(raw, str) else None


def _model_supports_tool_call(obj: object) -> bool:
    """缺 tool_call 或 tool_call != False 视为支持。"""
    if not isinstance(obj, dict):
        return False
    tc = obj.get("tool_call")
    return tc is not False


def candidate_providers(api_data: dict) -> dict[str, ProviderInfo]:
    """解析 api.json 为候选供应商映射（id → ProviderInfo）。

    仅保留 npm 为 ``@ai-sdk/openai-compatible`` 的条目；models 只保留
    支持工具调用的 id，顺序与接口一致。
    """
    result: dict[str, ProviderInfo] = {}
    for pid, raw in api_data.items():
        if not isinstance(raw, dict):
            continue
        if raw.get("npm") != _NPM_OPENAI_COMPATIBLE:
            continue
        name = raw.get("name")
        api = raw.get("api")
        if not isinstance(name, str) or not name:
            name = pid
        if not isinstance(api, str) or not api:
            # 少数供应商无 api 模板（用官方 openai base）；保留空串占位
            api = ""
        env = raw.get("env")
        env_list = [str(x) for x in env] if isinstance(env, list) else []

        models_raw = raw.get("models")
        models = [
            mid for m in (models_raw.values() if isinstance(models_raw, dict) else ())
            if (mid := _model_id(m)) and _model_supports_tool_call(m)
        ]

        result[pid] = ProviderInfo(
            id=pid,
            name=name,
            base_url=api,
            env=env_list,
            models=models,
        )
    return result


def all_model_ids(api_data: dict) -> dict[str, list[str]]:
    """兼容便捷函数：返回候选供应商 id → 模型 id 列表。"""
    return {pid: list(p.models) for pid, p in candidate_providers(api_data).items()}


def resolve_base_url(template: str, values: dict[str, str]) -> str:
    """渲染 api 模板中的 ``${VAR}``（值缺失替换为空串）。"""
    if not isinstance(values, dict):
        values = {}

    def _sub(m: re.Match[str]) -> str:
        return str(values.get(m.group(1), ""))

    return _VAR_RE.sub(_sub, template)


def is_secret_env_var(name: str) -> bool:
    """env 变量名是否为密钥类（含 KEY / TOKEN / PAT）。"""
    upper = str(name).upper()
    return (
        "KEY" in upper
        or "TOKEN" in upper
        or "PAT" in upper
    )