"""
提供商配置模块。

管理 ``{MYCODE_HOME_DIR}/config.toml`` 中的 ``[providers.<id>]`` section
与当前模型（``model_provider`` / ``model``），以及旧顶层
``api_key`` / ``base_url`` / ``model_name`` 的一次性自动迁移。

设计要点：

- 用 ``tomlkit`` 读写，保留用户手写配置的注释与格式；只增删改目标
  section 与顶层两个键。
- 提供商 section 的字段：

  - ``name`` / ``base_url`` / ``api_key``：显示名与连接信息；
  - ``enabled_models``：已启用的模型 id 列表（如
    ``["gpt-4o", "gpt-4o-mini"]``）；
  - ``send_reasoning_content`` / ``extra_body``：**提供商级**的默认请求
    设置（``/provider`` 的「修改设定值」维护），键缺失表示未配置。

  后两项与模型级同名配置的**回退顺序**是：模型级 → 提供商级 → models.dev
  缓存推导（仅 ``interleaved.field == "reasoning_content"`` 视为开启），
  见 ``resolve_send_reasoning`` / ``resolve_extra_body``。
- 模型级配置（``/provider`` 的「模型配置」菜单）存在
  ``[providers.<id>.models.<model_id>]`` 子表中：

  - ``name``：模型显示名（空表示用 models.dev 缓存里的名字 / 模型 id）；
  - ``send_reasoning_content``：是否把 ``reasoning_content`` 回传给模型。
    键缺失表示「未显式配置」，回退到 models.dev 缓存里该模型的
    ``interleaved`` 推导值（仅 ``{"field": "reasoning_content"}`` 为真）；
  - ``extra_body``：JSON 字符串，请求时作为 OpenAI 客户端的 ``extra_body``
    透传给服务端。

  子表名即模型 id；含 ``.`` / ``/`` 等键字符的模型 id 写入时由 ``tomlkit``
  自动加引号，读取时按原样匹配。
- 提供商 id 两种命名：
  - models.dev 的提供商用其原始 id（如 ``deepseek``）；
  - 自定义 / 迁移的提供商用 ``udf-provider-N``（``next_user_defined_id``
    取未占用的最小 N）。
- ``migrate_legacy()``：顶层 ``api_key`` / ``base_url`` 任一非空时，迁移
  为一个 ``udf-provider-N`` 提供商（name 取 base_url 的 host，models 取
  旧 ``model_name``），写 ``model_provider`` / ``model``，
  删除顶层 ``api_key`` / ``base_url``。环境变量不参与迁移。
- 写回后调用 ``config.invalidate()`` 失效缓存，让 ``config.get`` 立即可见。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import urlparse
from typing import Any, Optional

import tomlkit

from mycode import config


@dataclass
class ProviderConfig:
    """单个提供商配置。

    ``send_reasoning_content`` / ``extra_body`` 为该提供商的**默认**请求
    设置，模型级同名配置优先于它们（见 ``resolve_send_reasoning`` /
    ``resolve_extra_body``）。``None`` 表示未配置。
    """

    id: str
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    models: list[str] = field(default_factory=list)
    send_reasoning_content: Optional[bool] = None
    extra_body: Optional[dict] = None


@dataclass
class ModelConfig:
    """单个模型的配置（``/provider`` 的「模型配置」菜单维护）。

    Attributes:
        id: 模型 id。
        name: 显示名（空串表示未覆盖，用 models.dev 名字 / 模型 id）。
        send_reasoning_content: 是否回传 ``reasoning_content``；``None``
            表示未显式配置，按 models.dev 缓存的 ``interleaved`` 推导。
        extra_body: 额外请求体（JSON 对象），请求时作为 ``extra_body``
            透传；``None`` / 空表示不传。
    """

    id: str
    name: str = ""
    send_reasoning_content: Optional[bool] = None
    extra_body: Optional[dict] = None


def _document() -> tomlkit.TOMLDocument:
    """读取 config.toml 为 tomlkit 文档（不存在返回空文档）。"""
    try:
        with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
            return tomlkit.parse(f.read())
    except FileNotFoundError:
        return tomlkit.document()
    except Exception:
        # 解析失败时忽略：读操作按空处理（写操作会覆盖）
        return tomlkit.document()


def _write_document(doc: tomlkit.TOMLDocument) -> None:
    """写回 config.toml 并保证父目录存在。"""
    import os

    path = config.CONFIG_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(doc))
    config.invalidate()


# 提供商 section 的 4 个固定字段；其余键（models 模型级配置）在 save 时保留
_PROVIDER_FIELDS = ("name", "base_url", "api_key", "enabled_models",
                    "send_reasoning_content", "extra_body")


def _provider_table(id_: str) -> tomlkit.items.Table:
    """构造空提供商 section 表。"""
    tbl = tomlkit.table()
    tbl.append(tomlkit.key("name"), "")  # placeholder, replaced later
    return tbl


def save_provider(p: ProviderConfig) -> None:
    """保存（新增或更新）一个提供商 section，保留文档其余注释与键。

    ``[providers.<id>]`` 下除 4 个提供商字段外的其他键（当前是
    ``models`` 模型级配置子表）原样保留——``section.clear()`` 前
    先把它们取出、重建后放回，避免编辑提供商设定值时丢掉模型级配置。
    """
    doc = _document()
    tables = doc.get("providers")
    if not isinstance(tables, tomlkit.items.Table):
        tables = tomlkit.table()
        doc["providers"] = tables

    section = tables.get(p.id)
    if section is None or not isinstance(section, tomlkit.items.Table):
        section = tomlkit.table()
        tables[p.id] = section

    # 暂存非提供商字段的子表（models 等），clear 后放回
    keep = {k: v for k, v in section.items()
            if k not in _PROVIDER_FIELDS}
    # 清空重建该 section 内容（保持简洁稳定）
    section.clear()
    section["name"] = p.name
    section["base_url"] = p.base_url
    section["api_key"] = p.api_key
    section["enabled_models"] = p.models
    if p.send_reasoning_content is not None:
        section["send_reasoning_content"] = p.send_reasoning_content
    if p.extra_body:
        section["extra_body"] = json.dumps(
            p.extra_body, ensure_ascii=False, sort_keys=True)
    section.update(keep)

    _write_document(doc)


def _to_provider_config(pid: str, section: Any) -> ProviderConfig:
    """提供商 section → ProviderConfig（未配置的键取缺省值）。"""
    enabled = section.get("enabled_models", [])
    flag = section.get("send_reasoning_content")
    return ProviderConfig(
        id=pid,
        name=str(section.get("name", "")),
        base_url=str(section.get("base_url", "")),
        api_key=str(section.get("api_key", "")),
        models=[str(x) for x in enabled] if isinstance(enabled, list) else [],
        send_reasoning_content=flag if isinstance(flag, bool) else None,
        extra_body=_parse_extra_body(section.get("extra_body")),
    )


def load_providers() -> dict[str, ProviderConfig]:
    """读取全部 [providers.*] section。"""
    tables = _document().get("providers")
    if not isinstance(tables, tomlkit.items.Table):
        return {}
    return {
        str(pid): _to_provider_config(str(pid), section)
        for pid, section in tables.items()
        if isinstance(section, tomlkit.items.Table)
    }


def delete_provider(pid: str) -> None:
    """删除指定提供商 section（不存在则空操作）。"""
    doc = _document()
    tables = doc.get("providers")
    if isinstance(tables, tomlkit.items.Table) and pid in tables:
        del tables[pid]
        _write_document(doc)


# ---------------------------------------------------------------------------
# 模型级配置（[providers.<id>.models.<model_id>]）
# ---------------------------------------------------------------------------

def _parse_extra_body(raw: object) -> Optional[dict]:
    """解析 extra_body：TOML 里的 JSON 字符串 → dict；非法/空返回 None。"""
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _to_model_config(mid: str, sub: Any) -> ModelConfig:
    """模型级配置子表 → ModelConfig。

    ``send_reasoning_content`` 非布尔（缺键 / 脏数据）时为 ``None``——
    表示未显式配置，由 ``resolve_send_reasoning`` 按 models.dev 缓存推导。
    """
    flag = sub.get("send_reasoning_content")
    return ModelConfig(
        id=mid,
        name=str(sub.get("name", "")),
        send_reasoning_content=flag if isinstance(flag, bool) else None,
        extra_body=_parse_extra_body(sub.get("extra_body")),
    )


def _model_config_table(pid: str) -> Any:
    """取某提供商的模型级配置子表；链路任一环非表则返回 None。"""
    tables = _document().get("providers")
    if not isinstance(tables, tomlkit.items.Table):
        return None
    section = tables.get(pid)
    if not isinstance(section, tomlkit.items.Table):
        return None
    return section.get("models")


def load_model_configs(pid: str) -> dict[str, ModelConfig]:
    """读取某提供商的模型级配置（模型 id → ModelConfig）。

    ``send_reasoning_content`` 键缺失时为 ``None``（未显式配置，由
    ``resolve_send_reasoning`` 按 models.dev 缓存推导）；``extra_body``
    非法 JSON 时同样为 ``None``。
    """
    cfgs = _model_config_table(pid)
    if not isinstance(cfgs, tomlkit.items.Table):
        return {}
    return {
        str(mid): _to_model_config(str(mid), sub)
        for mid, sub in cfgs.items()
        if isinstance(sub, tomlkit.items.Table)
    }


def save_model_config(pid: str, mc: ModelConfig) -> None:
    """保存单个模型的配置到 ``[providers.<id>.models.<model_id>]``。

    三个字段全部为空（显示名空、开关未配置、extra_body 空）时删除该
    模型子表，避免留下无意义的空配置。提供商不存在时不写入。

    各字段都是**按需写入**：值为空 / 未配置时该键不落到 TOML（与
    ``send_reasoning_content`` / ``extra_body`` 一致），读取时按缺省值
    处理，不留 ``name = ""`` 这类空噪音。
    """
    doc = _document()
    tables = doc.get("providers")
    if not isinstance(tables, tomlkit.items.Table) or pid not in tables:
        return
    section = tables[pid]
    if not isinstance(section, tomlkit.items.Table):
        return
    cfgs = section.get("models")
    if not isinstance(cfgs, tomlkit.items.Table):
        cfgs = tomlkit.table()
        section["models"] = cfgs

    if not (mc.name or mc.send_reasoning_content is not None or mc.extra_body):
        cfgs.pop(mc.id, None)
    else:
        sub = tomlkit.table()
        if mc.name:
            sub["name"] = mc.name
        if mc.send_reasoning_content is not None:
            sub["send_reasoning_content"] = mc.send_reasoning_content
        if mc.extra_body:
            sub["extra_body"] = json.dumps(
                mc.extra_body, ensure_ascii=False, sort_keys=True)
        cfgs[mc.id] = sub
    _write_document(doc)


def resolve_send_reasoning(pid: str, model: str) -> bool:
    """该模型是否应把 ``reasoning_content`` 回传给模型。

    逐级回退：模型级配置 → 提供商级配置 → models.dev 缓存里该模型的
    ``interleaved`` 推导值（仅 ``{"field": "reasoning_content"}`` 为真）。
    三处都未配置时为 False。
    """
    explicit = load_model_configs(pid).get(model)
    if explicit is not None and explicit.send_reasoning_content is not None:
        return explicit.send_reasoning_content
    provider_flag = load_providers().get(pid)
    if provider_flag is not None \
            and provider_flag.send_reasoning_content is not None:
        return provider_flag.send_reasoning_content
    return default_send_reasoning(pid, model)


def _cached_model_info(pid: str, model: str) -> Any:
    """从 models.dev 缓存取模型信息；缓存/提供商/模型任一缺失返回 None。"""
    from mycode import models_registry as mr

    data = mr.load_cached_api()
    if data is None:
        return None
    info = mr.candidate_providers(data).get(pid)
    return info.models.get(model) if info is not None else None


def default_send_reasoning(pid: str, model: str) -> bool:
    """按 models.dev 缓存推导模型是否默认回传思考内容。"""
    model_info = _cached_model_info(pid, model)
    return bool(model_info and model_info.interleaves_reasoning)


def resolve_extra_body(pid: str, model: str) -> Optional[dict]:
    """取该模型的 extra_body：模型级配置优先，其次提供商级（都无则 None）。"""
    cfg = load_model_configs(pid).get(model)
    if cfg is not None and cfg.extra_body:
        return cfg.extra_body
    provider = load_providers().get(pid)
    return provider.extra_body if provider is not None else None


def resolve_model_name(pid: str, model: str) -> str:
    """取该模型的显示名。

    优先 ``/provider`` 里配置的显示名；为空时回退 models.dev 缓存中的
    模型名；都没有则用模型 id。
    """
    cfg = load_model_configs(pid).get(model)
    if cfg is not None and cfg.name:
        return cfg.name
    model_info = _cached_model_info(pid, model)
    return (model_info.name if model_info is not None and model_info.name
            else model)


def get_current() -> tuple[str, str] | None:
    """返回 (provider_id, model_id)；未配置返回 None。"""
    pid = config.get("model_provider")
    model = config.get("model")
    if pid and model:
        return (pid, model)
    return None


def set_current(provider_id: str, model_id: str) -> None:
    """写回 model_provider 与 model（保留文档其余内容）。"""
    doc = _document()
    doc["model_provider"] = provider_id
    doc["model"] = model_id
    _write_document(doc)


def is_configured() -> bool:
    """是否存在至少一个已配置提供商。"""
    return bool(load_providers())


def next_user_defined_id() -> str:
    """取未占用的最小 ``udf-provider-N``。"""
    providers = load_providers()
    n = 1
    while f"udf-provider-{n}" in providers:
        n += 1
    return f"udf-provider-{n}"


def is_user_defined(pid: str) -> bool:
    """是否为自定义提供商（``udf-`` 前缀命名）。"""
    return pid.startswith("udf-")


def rename_provider(old_id: str, new_id: str) -> Optional[str]:
    """重命名自定义提供商 id；返回新 id（无需改名返回 None，冲突返回 None）。

    同步处理：

    - ``[providers.<old>]`` section 键改为 ``<new>``；
    - 若该提供商是当前 ``model_provider``，顶层键同步改为新 id；
    - 写回后 ``config.invalidate()``，``config.get`` 立即可见（内存中
      读到的当前提供商随之更新）。
    """
    new_id = (new_id or "").strip()
    if not new_id or new_id == old_id:
        return None
    full_new = f"udf-{new_id}"
    if not old_id.startswith("udf-") or full_new == old_id:
        return None
    doc = _document()
    tables = doc.get("providers")
    if not isinstance(tables, tomlkit.items.Table) or old_id not in tables:
        return None
    if full_new in tables:
        return None  # 目标 id 已被占用

    tables[full_new] = tables.pop(old_id)
    pid, model = get_current() or ("", "")
    if pid == old_id and model:
        doc["model_provider"] = full_new
    _write_document(doc)
    return full_new


def _host_of(base_url: str) -> str:
    """提取 base_url 的 host；空或失败返回空串。"""
    try:
        return urlparse(base_url).hostname or ""
    except Exception:
        return ""


def migrate_legacy() -> Optional[str]:
    """一次迁移顶层 legacy 配置；无 legacy 返回 None，否则返回新 provider id。

    条件：顶层 ``api_key`` 或 ``base_url`` 任一非空。生成
    ``[providers.udf-provider-N]``（name = base_url host，models = 旧
    model_name 列表），写 ``model_provider`` / ``model``，
    删除顶层 api_key / base_url。
    """
    doc = _document()
    api_key = doc.get("api_key")
    base_url = doc.get("base_url")
    if not isinstance(api_key, str):
        api_key = ""
    if not isinstance(base_url, str):
        base_url = ""

    has_legacy = bool(api_key.strip()) or bool(base_url.strip())
    if not has_legacy:
        return None

    model_name_raw = doc.get("model_name")
    model_name = str(model_name_raw) if isinstance(model_name_raw, str) else ""
    pid = next_user_defined_id()
    # 迁移成 provider section（name 用 host，缺省用 pid）
    name = _host_of(base_url) or pid
    p = ProviderConfig(
        id=pid,
        name=name,
        base_url=base_url.strip(),
        api_key=api_key.strip(" "),
        models=[model_name] if model_name else [],
    )
    save_provider(p)
    # 重新读回（save 已写盘），补顶层当前模型与删除 legacy 键
    doc = _document()
    if model_name:
        doc["model_provider"] = pid
        doc["model"] = model_name
    for key in ("api_key", "base_url"):
        if key in doc:
            del doc[key]
    config.invalidate()
    # 直接写入而非 save_provider（避免重复写），仍需 _write_document 语义
    import os

    path = config.CONFIG_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(doc))
    config.invalidate()
    return pid