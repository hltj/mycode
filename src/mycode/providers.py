"""
供应商配置模块。

管理 ``{MYCODE_HOME_DIR}/config.toml`` 中的 ``[providers.<id>]`` section
与当前模型（``model_provider`` / ``model_name``），以及旧顶层
``api_key`` / ``base_url`` / ``model_name`` 的一次性自动迁移。

设计要点：

- 用 ``tomlkit`` 读写，保留用户手写配置的注释与格式；只增删改目标
  section 与顶层两个键。
- 供应商 id 两种命名：
  - models.dev 的供应商用其原始 id（如 ``deepseek``）；
  - 自定义 / 迁移的供应商用 ``user-defined-N``（``next_user_defined_id``
    取未占用的最小 N）。
- ``migrate_legacy()``：顶层 ``api_key`` / ``base_url`` 任一非空时，迁移
  为一个 ``user-defined-N`` 供应商（name 取 base_url 的 host，models 取
  旧 ``model_name``），保留 ``model_name`` 并补 ``model_provider``，
  删除顶层 ``api_key`` / ``base_url``。环境变量不参与迁移。
- 写回后调用 ``config.invalidate()`` 失效缓存，让 ``config.get`` 立即可见。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse
from typing import Optional

import tomlkit

from mycode import config


@dataclass
class ProviderConfig:
    """单个供应商配置。"""

    id: str
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    models: list[str] = field(default_factory=list)


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


def _provider_table(id_: str) -> tomlkit.items.Table:
    """构造空供应商 section 表。"""
    tbl = tomlkit.table()
    tbl.append(tomlkit.key("name"), "")  # placeholder, replaced later
    return tbl


def save_provider(p: ProviderConfig) -> None:
    """保存（新增或更新）一个供应商 section，保留文档其余注释与键。"""
    doc = _document()
    tables = doc.get("providers")
    if not isinstance(tables, tomlkit.items.Table):
        tables = tomlkit.table()
        doc["providers"] = tables

    section = tables.get(p.id)
    if section is None or not isinstance(section, tomlkit.items.Table):
        section = tomlkit.table()
        tables[p.id] = section

    # 清空重建该 section 内容（保持简洁稳定）
    section.clear()
    section["name"] = p.name
    section["base_url"] = p.base_url
    section["api_key"] = p.api_key
    section["models"] = p.models

    _write_document(doc)


def load_providers() -> dict[str, ProviderConfig]:
    """读取全部 [providers.*] section。"""
    doc = _document()
    tables = doc.get("providers")
    result: dict[str, ProviderConfig] = {}
    if not isinstance(tables, tomlkit.items.Table):
        return result
    for pid, section in tables.items():
        if not isinstance(section, tomlkit.items.Table):
            continue
        models_raw = section.get("models", [])
        models = [str(x) for x in models_raw] if isinstance(models_raw, list) else []
        result[str(pid)] = ProviderConfig(
            id=str(pid),
            name=str(section.get("name", "")),
            base_url=str(section.get("base_url", "")),
            api_key=str(section.get("api_key", "")),
            models=models,
        )
    return result


def delete_provider(pid: str) -> None:
    """删除指定供应商 section（不存在则空操作）。"""
    doc = _document()
    tables = doc.get("providers")
    if isinstance(tables, tomlkit.items.Table) and pid in tables:
        del tables[pid]
        _write_document(doc)


def get_current() -> tuple[str, str] | None:
    """返回 (provider_id, model_name)；未配置返回 None。"""
    pid = config.get("model_provider")
    model = config.get("model_name")
    if pid and model:
        return (pid, model)
    return None


def set_current(provider_id: str, model_name: str) -> None:
    """写回 model_provider 与 model_name（保留文档其余内容）。"""
    doc = _document()
    doc["model_provider"] = provider_id
    doc["model_name"] = model_name
    _write_document(doc)


def is_configured() -> bool:
    """是否存在至少一个已配置供应商。"""
    return bool(load_providers())


def next_user_defined_id() -> str:
    """取未占用的最小 ``user-defined-N``。"""
    providers = load_providers()
    n = 1
    while f"user-defined-{n}" in providers:
        n += 1
    return f"user-defined-{n}"


def _host_of(base_url: str) -> str:
    """提取 base_url 的 host；空或失败返回空串。"""
    try:
        return urlparse(base_url).hostname or ""
    except Exception:
        return ""


def migrate_legacy() -> Optional[str]:
    """一次迁移顶层 legacy 配置；无 legacy 返回 None，否则返回新 provider id。

    条件：顶层 ``api_key`` 或 ``base_url`` 任一非空。生成
    ``[providers.user-defined-N]``（name = base_url host，models = 旧
    model_name 列表），保留 model_name 并补 model_provider，
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
        doc["model_name"] = model_name
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