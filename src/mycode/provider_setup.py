"""
模型提供商配置流程模块。

``/provider`` 模型提供商管理流程的编排层：组合 ``ask_ui``（主菜单与二级
菜单）、``filter_ui``（候选筛选、模型多选）、``form_ui``（变量/自定义表单），
把用户操作落到 ``providers`` 与 ``providers.set_current``。

设计详见 ``docs/dev/provider_setup_design.md``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mycode import models_registry as mr
from mycode import providers as pv
from mycode.ask_ui import AskOption, AskQuestion, ask_ui
from mycode.filter_ui import FilterOption, FilterResult, filter_ui
from mycode.form_ui import FormField, FormResult, form_ui

# 每个提供商可勾选启用的模型上限
MAX_MODELS_PER_PROVIDER = 15

# 主菜单 / 二级菜单 action 标识
MAIN_ADD_CATALOG = "add_from_catalog"
MAIN_ADD_CUSTOM = "add_custom"
MAIN_CANCEL = "cancel"
EDIT_PREFIX = "edit:"

EDIT_VARS = "edit_vars"
EDIT_MODELS = "edit_models"
EDIT_DELETE = "edit_delete"
EDIT_BACK = "edit_back"


@dataclass
class _MetaStatus:
    """主菜单里“添加模型提供商”的描述文案基础数据。"""

    text: str


def _current_style():
    from mycode.renderer import _get_renderer
    return _get_renderer().create_prompt_style()


# ---------------------------------------------------------------------------
# 模型库状态 → 描述
# ---------------------------------------------------------------------------

def _set_meta_status(meta: dict) -> None:
    """写入模型库 meta（测试钩子；生产流程由 models_registry 直接维护）。"""
    mr.save_meta(meta)


def _meta_status_text() -> str:
    """从 meta 与候选 provider 数量生成“添加模型提供商”的描述。"""
    meta = mr.load_meta()
    status = meta.get("status")
    updated = meta.get("updated_at")
    if updated and status in ("success", "not_modified"):
        n = meta.get("providers_count")
        if not isinstance(n, int) or n <= 0:
            n = len(candidate_providers())
        if n:
            return f"模型库更新于 {updated[:16]} · {n} 家可用"
        return "模型库更新于今天（暂无可解析的候选）"
    if status == "error":
        return f"更新失败：{meta.get('error', '未知错误')}"
    return "模型库尚未就绪"


def candidate_providers() -> dict[str, mr.ProviderInfo]:
    """从缓存解析候选模型提供商（缓存缺失返回空）。"""
    data = mr.load_cached_api()
    if data is None:
        return {}
    return mr.candidate_providers(data)


# ---------------------------------------------------------------------------
# 选项构造
# ---------------------------------------------------------------------------

def _main_menu_question(existing: dict[str, pv.ProviderConfig]) -> AskQuestion:
    """构造 /provider 主菜单问题。"""
    opts = [
        AskOption(
            label="添加模型提供商",
            value=MAIN_ADD_CATALOG,
            description=_meta_status_text(),
        ),
        AskOption(label="添加自定义模型提供商", value=MAIN_ADD_CUSTOM),
    ]
    for pid in sorted(existing):
        p = existing[pid]
        n = len(p.models)
        opts.append(AskOption(
            label=f"编辑：{pid}",
            value=f"{EDIT_PREFIX}{pid}",
            description=f"{p.name} · {n} 模型",
        ))
    opts.append(AskOption(label="取消", value=MAIN_CANCEL))
    return AskQuestion(title="模型提供商配置", options=opts)


def _candidate_options(infos: dict[str, mr.ProviderInfo]) -> list[FilterOption]:
    """候选提供商 → filter_ui options（label 名称 (id) · N 模型）。"""
    return [
        FilterOption(
            label=f"{infos[pid].name} ({pid}) · {len(infos[pid].models)} 模型",
            value=pid,
        )
        for pid in sorted(infos)
    ]


def _model_options(models: list[str], names: dict[str, str]) -> list[FilterOption]:
    """模型 id → filter_ui options（label 模型名称（模型id））。"""
    return [
        FilterOption(label=f"{names.get(m, m)}（{m}）", value=m)
        for m in models
    ]


def _cap_models(models: list[str]) -> list[str]:
    """勾选上限：截断保留前 MAX_MODELS_PER_PROVIDER 个。"""
    return models[:MAX_MODELS_PER_PROVIDER]


# ---------------------------------------------------------------------------
# 添加提供商
# ---------------------------------------------------------------------------

def add_from_catalog() -> Optional[str]:
    """添加一个 models.dev 候选提供商；返回新 provider id（取消返回 None）。"""
    infos = candidate_providers()
    if not infos:
        return None
    candidates = _candidate_options(infos)
    pick = filter_ui(
        candidates,
        title="选择模型提供商",
        description="输入关键词筛选，按 Enter 进列表，再 Enter 选定",
        style=_current_style(),
    )
    if pick.aborted or not pick.selected:
        return None
    pid = pick.selected[0]
    info = infos[pid]

    # 变量表单：env 列表逐变量填写
    fields: list[FormField] = []
    for var in info.env:
        is_secret = mr.is_secret_env_var(var)
        hint: str = ""
        if f"${{{var}}}" in info.base_url:
            hint = "用于拼接 API 地址"
        fields.append(FormField(
            name=var,
            label=var,
            hint=hint,
            password=is_secret,
            placeholder="输入密钥" if is_secret else "输入值",
        ))
    # 无 env 变量（如某些本地服务）跳过表单
    values: dict[str, str] = {}
    if fields:
        form = form_ui(
            fields,
            title=f"配置 {info.name}",
            description="填写调用该提供商所需的变量，密钥类以掩码显示",
            style=_current_style(),
        )
        if form.aborted:
            return None
        values = form.values

    # 渲染 base_url
    base_url = mr.resolve_base_url(info.base_url, values)
    # 密钥：取第一个填值且变量名为密钥类的
    api_key = next(
        (values[var] for var in info.env
         if var in values and values[var].strip() and mr.is_secret_env_var(var)),
        "",
    )

    # 模型多选（候选全部可勾选，上限 15）
    model_names = _model_names_for_provider(info.id)
    model_opts = _model_options(info.models, model_names)
    pick_models = filter_ui(
        model_opts,
        title=f"勾选 {info.name} 的模型",
        description=f"可勾选多个（上限 {MAX_MODELS_PER_PROVIDER}）；"
                    f"输入关键词筛选，空格勾选，Enter 完成",
        multi=True,
        style=_current_style(),
    )
    if pick_models.aborted or not pick_models.selected:
        return None
    models = _cap_models(pick_models.selected)

    pv.save_provider(pv.ProviderConfig(
        id=pid, name=info.name, base_url=base_url,
        api_key=api_key, models=models,
    ))
    if not pv.get_current():
        pv.set_current(pid, models[0])
    return pid


def add_custom() -> Optional[str]:
    """添加自定义模型提供商；返回新 provider id（取消/模型列表空返回 None）。"""
    fields = [
        FormField(name="name", label="显示名",
                  placeholder="留空则取 Base URL 的 host"),
        FormField(name="base_url", label="Base URL", required=True,
                  placeholder="https://api.example.com/v1"),
        FormField(name="api_key", label="API Key", password=True,
                  placeholder="本地服务可留空"),
        FormField(name="models", label="模型列表", required=True,
                  placeholder="逗号分隔，如 gpt-4o,gpt-4o-mini"),
    ]
    form = form_ui(
        fields,
        title="添加自定义模型提供商",
        description="Base URL 必填；模型列表逗号分隔、至少一个",
        style=_current_style(),
    )
    if form.aborted:
        return None
    name = form.values.get("name", "").strip()
    base_url = form.values.get("base_url", "").strip()
    api_key = form.values.get("api_key", "")
    models_text = form.values.get("models", "")
    models = _parse_model_list(models_text)
    if not base_url or not models:
        return None
    if not name:
        name = _host_of(base_url)
    pid = pv.next_user_defined_id()
    pv.save_provider(pv.ProviderConfig(
        id=pid, name=name, base_url=base_url,
        api_key=api_key, models=models,
    ))
    if not pv.get_current():
        pv.set_current(pid, models[0])
    return pid


def _parse_model_list(text: str) -> list[str]:
    """逗号分隔模型列表 → 去空白去重的 list。"""
    return [x for x in (s.strip() for s in text.split(",")) if x]


def _host_of(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _model_names_for_provider(pid: str) -> dict[str, str]:
    """从缓存取该 provider 的模型 id → 名称映射。"""
    data = mr.load_cached_api()
    if data is None:
        return {}
    raw = data.get(pid)
    if not isinstance(raw, dict):
        return {}
    models = raw.get("models")
    if not isinstance(models, dict):
        return {}
    result = {}
    for mid, m in models.items():
        if isinstance(m, dict) and isinstance(m.get("name"), str):
            result[mid] = m["name"]
    return result


# ---------------------------------------------------------------------------
# 编辑
# ---------------------------------------------------------------------------

def edit_variables(pid: str) -> None:
    """编辑提供商的 base_url / api_key / 模型列表。"""
    existing = pv.load_providers().get(pid)
    if existing is None:
        return
    fields = [
        FormField(name="base_url", label="Base URL", initial=existing.base_url),
        FormField(name="api_key", label="API Key", initial=existing.api_key,
                  password=True),
        FormField(name="models", label="模型列表",
                  initial=",".join(existing.models)),
    ]
    form = form_ui(fields, title=f"编辑 {pid}", style=_current_style())
    if form.aborted:
        return
    models = _parse_model_list(form.values.get("models", ""))
    base_url = form.values.get("base_url", "").strip()
    if not base_url or not models:
        return
    existing.base_url = base_url
    existing.api_key = form.values.get("api_key", "")
    existing.models = models
    pv.save_provider(existing)


def edit_reselect_models(pid: str) -> None:
    """重选提供商勾选的模型。"""
    existing = pv.load_providers().get(pid)
    if existing is None:
        return
    infos = candidate_providers()
    if pid in infos:
        names = _model_names_for_provider(pid)
        models = infos[pid].models
    else:
        names = {}
        models = existing.models
    # 回显现有勾选
    opts = _model_options(models, names)
    for o in opts:
        if o.value in existing.models:
            o.selected = True
    pick = filter_ui(opts, title=f"勾选 {pid} 的模型",
                     description=f"可勾选多个（上限 {MAX_MODELS_PER_PROVIDER}）",
                     multi=True, style=_current_style())
    if pick.aborted:
        return
    existing.models = _cap_models(pick.selected)
    pv.save_provider(existing)
    # 若当前模型被取消勾选，清空当前
    pid_cur, model_cur = pv.get_current() or ("", "")
    if pid_cur == pid and model_cur not in existing.models:
        pv.set_current("", "")


def delete_provider(pid: str) -> None:
    """删除提供商；若是当前提供商则清空当前模型。"""
    pv.delete_provider(pid)
    pid_cur, _ = pv.get_current() or ("", "")
    if pid_cur == pid:
        pv.set_current("", "")


def _edit_menu_question(pid: str, existing: pv.ProviderConfig) -> AskQuestion:
    """构造编辑二级菜单。"""
    return AskQuestion(
        title=f"编辑：{pid}",
        options=[
            AskOption(label="修改变量", value=EDIT_VARS),
            AskOption(label="重选模型", value=EDIT_MODELS),
            AskOption(label="删除模型提供商", value=EDIT_DELETE),
            AskOption(label="返回", value=EDIT_BACK),
        ],
    )


def run_provider_setup() -> None:
    """运行 /provider 主流程（循环直到取消）。"""
    while True:
        existing = pv.load_providers()
        q = _main_menu_question(existing)
        result = ask_ui([q], style=_current_style())
        if result.aborted:
            return
        answer = result.answers[0]
        value = answer.selected[0] if answer.selected else MAIN_CANCEL
        if value == MAIN_CANCEL:
            return
        if value == MAIN_ADD_CATALOG:
            add_from_catalog()
        elif value == MAIN_ADD_CUSTOM:
            add_custom()
        elif value.startswith(EDIT_PREFIX):
            pid = value[len(EDIT_PREFIX):]
            _run_edit_loop(pid)
        else:
            return


def _run_edit_loop(pid: str) -> None:
    while True:
        existing = pv.load_providers().get(pid)
        if existing is None:
            return
        q = _edit_menu_question(pid, existing)
        result = ask_ui([q], style=_current_style())
        if result.aborted:
            return
        answer = result.answers[0]
        value = answer.selected[0] if answer.selected else EDIT_BACK
        if value == EDIT_BACK:
            return
        if value == EDIT_VARS:
            edit_variables(pid)
        elif value == EDIT_MODELS:
            edit_reselect_models(pid)
        elif value == EDIT_DELETE:
            delete_provider(pid)
            return