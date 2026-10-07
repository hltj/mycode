"""
模型提供商配置流程模块。

``/provider`` 模型提供商管理流程的编排层：组合 ``ask_ui``（主菜单与二级
菜单）、``filter_ui``（候选筛选、模型多选）、``form_ui``（变量/自定义表单），
把用户操作落到 ``providers`` 与 ``providers.set_current``。

设计详见 ``docs/dev/provider_setup_design.md``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional

from mycode import models_registry as mr
from mycode import providers as pv
from mycode.ask_ui import AskOption, AskQuestion, ask_ui
from mycode.filter_ui import FilterOption, FilterResult, filter_ui
from mycode.form_ui import FormField, FormResult, form_ui

# 每个提供商可勾选启用的模型上限
MAX_MODELS_PER_PROVIDER = 15

# 主菜单 / 二级菜单 action 标识
MAIN_ADD_CATALOG = "add_from_catalog"
MAIN_ADD_USER_DEFINED = "add_user_defined"
MAIN_CANCEL = "cancel"
EDIT_PREFIX = "edit:"

EDIT_VARS = "edit_vars"
EDIT_MODELS = "edit_models"
EDIT_MODEL_CONFIG = "edit_model_config"
EDIT_DELETE = "edit_delete"
EDIT_BACK = "edit_back"

# 模型配置三级菜单
MODEL_CONFIG_PREFIX = "model_config:"
MODEL_CONFIG_BACK = "model_config_back"

# 删除确认
CONFIRM_DELETE = "confirm_delete"
CONFIRM_CANCEL = "confirm_cancel"


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
        AskOption(label="添加自定义模型提供商", value=MAIN_ADD_USER_DEFINED),
        *[
            AskOption(
                label=f"编辑：{existing[pid].name}",
                value=f"{EDIT_PREFIX}{pid}",
                description=f"{pid} · {len(existing[pid].models)} 模型",
            )
            for pid in sorted(existing)
        ],
        AskOption(label="返回", value=MAIN_CANCEL),
    ]
    return AskQuestion(title="模型提供商配置", options=opts)


def _candidate_options(
    infos: dict[str, mr.ProviderInfo],
    existing: dict[str, pv.ProviderConfig] | None = None,
) -> list[FilterOption]:
    """候选提供商 → filter_ui options（label 名称（id · N 模型））。

    已添加的提供商在括号内 id 前标注「【已添加】」，模型数为
    ``{m}/{n}``（m = 已选个数，n = 候选总数）。
    """
    existing = existing or {}

    def label(pid: str) -> str:
        name, n = infos[pid].name, len(infos[pid].models)
        added = existing.get(pid)
        if added is None:
            return f"{name}（{pid} · {n} 模型）"
        return f"{name}（【已添加】{pid} · {len(added.models)}/{n} 模型）"

    return [FilterOption(label=label(pid), value=pid) for pid in sorted(infos)]


def _model_options(
    models: dict[str, mr.ModelInfo],
    selected: set[str] | None = None,
) -> list[FilterOption]:
    """模型 → filter_ui options（label 模型名称（模型id））。

    按显示名排序（不区分大小写；缺失回退 id），同名再按 id 保证稳定。
    ``selected`` 非空时对应选项初始勾选（回显现有勾选）。
    """
    def sort_key(m: str) -> tuple[str, str]:
        return ((models[m].name or m).lower(), m)

    selected = selected or set()
    return [
        FilterOption(
            label=f"{models[m].name or m}（{m}）",
            value=m,
            selected=m in selected,
        )
        for m in sorted(models, key=sort_key)
    ]


def _cap_models(models: list[str]) -> list[str]:
    """勾选上限：截断保留前 MAX_MODELS_PER_PROVIDER 个。"""
    return models[:MAX_MODELS_PER_PROVIDER]


# ---------------------------------------------------------------------------
# 添加提供商
# ---------------------------------------------------------------------------

def add_from_catalog() -> Optional[str]:
    """添加一个 models.dev 候选提供商；返回新 provider id（取消返回 None）。

    选中的候选若已添加：不重复走添加流程，进入该提供商的
    「编辑：提供商」二级菜单。
    """
    infos = candidate_providers()
    if not infos:
        return None
    candidates = _candidate_options(infos, pv.load_providers())
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

    if pid in pv.load_providers():
        # 已添加：进入「编辑：提供商」二级菜单
        _run_edit_loop(pid)
        return pid

    # 变量表单：env 列表逐变量填写
    fields = [
        FormField(
            name=var,
            label=var,
            hint="用于拼接 API 地址" if f"${{{var}}}" in info.base_url else "",
            password=is_secret,
            placeholder="sk-..." if is_secret else "输入值",
        )
        for var in info.env
        if (is_secret := mr.is_secret_env_var(var)) is not None
    ]
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
    pick_models = filter_ui(
        _model_options(info.models),
        title=f"勾选 {info.name}（{info.id}）的模型",
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


def _id_suffix_validator(exclude_pid: str = "") -> Callable[[str], Optional[str]]:
    """id 后缀校验器：留空合法（回退自动分配）；与现有 id 重复则阻止提交。

    ``exclude_pid`` 为编辑场景下当前提供商的完整 id（自身的后缀不算冲突）。
    """
    def _validate(text: str) -> Optional[str]:
        s = (text or "").strip()
        if not s:
            return None
        if f"udf-{s}" != exclude_pid and f"udf-{s}" in pv.load_providers():
            return f"udf-{s} 已被占用"
        return None
    return _validate


def add_user_defined() -> Optional[str]:
    """添加自定义模型提供商；返回新 provider id（取消/模型列表空返回 None）。"""
    default_suffix = pv.next_user_defined_id()[len("udf-"):]
    fields = [
        FormField(name="id_suffix", label="id 后缀", initial=default_suffix,
                  hint="提供商 id 为 udf-<后缀>",
                  validator=_id_suffix_validator()),
        FormField(name="name", label="显示名",
                  placeholder="留空则取 Base URL 的 host"),
        FormField(name="base_url", label="Base URL", required=True,
                  placeholder="https://api.example.com/v1"),
        FormField(name="api_key", label="API Key", password=True,
                  required=True, placeholder="sk-..."),
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
    suffix = form.values.get("id_suffix", "").strip()
    name = form.values.get("name", "").strip()
    base_url = form.values.get("base_url", "").strip()
    api_key = form.values.get("api_key", "")
    models_text = form.values.get("models", "")
    models = _parse_model_list(models_text)
    if not base_url or not models:
        return None
    if not name:
        name = _host_of(base_url)
    # 后缀留空：回退自动分配（取未占用的最小 N）；重名由校验器阻止
    pid = f"udf-{suffix}" if suffix else pv.next_user_defined_id()
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


# ---------------------------------------------------------------------------
# 编辑
# ---------------------------------------------------------------------------

def _inherited_flag_text(pid: str, is_udf: bool) -> str:
    """提供商级开关留空时的占位文字：说明回退来源与推导值。

    来源优先级：``provider_presets`` 预设值表（命中）> models.dev 推导。
    自定义提供商不在模型库中，一律「不回传：false」。
    """
    from mycode import provider_presets

    flag = provider_presets.lookup_config(pid).send_reasoning_content
    if isinstance(flag, bool):
        return f"true/false，留空=按提供商预置：{'true' if flag else 'false'}"
    if is_udf:
        return "true/false，留空=不回传：false"
    return "true/false，留空=按模型库默认"


def _inherited_extra_body_text(pid: str, model: str = "") -> str:
    """extra_body 留空时的占位文字：展示回退来源与内容。

    ``model`` 非空为模型级场景，先看提供商级配置；否则为提供商级场景。
    两者的下一级都是 ``provider_presets`` 提供商预置表。命中的配置压成
    单行展示（form_ui 是单行输入）；都没有则给通用示例。
    """
    from mycode import provider_presets

    if model:
        provider = pv.load_providers().get(pid)
        if provider is not None and provider.extra_body:
            return "留空=按提供商级配置：" + json.dumps(
                provider.extra_body, ensure_ascii=False)
    body = provider_presets.lookup_config(pid).extra_body
    if isinstance(body, dict) and body:
        return "留空=按提供商预置：" + json.dumps(body, ensure_ascii=False)
    return '如 DeepSeek 配置 {"thinking": {"type": "enabled"}}'


def edit_settings(pid: str) -> str:
    """编辑提供商的设定值：显示名 / base_url / api_key / 模型列表 /
    回传 reasoning_content / extra_body。

    后两项是**提供商级**的请求设置，模型级「模型配置」里的同名项优先
    于它们（见 ``providers.resolve_send_reasoning`` /
    ``resolve_extra_body``）。

    自定义提供商额外可编辑 id 后缀（提供商 id 为 ``udf-<后缀>``）；
    若该提供商是当前提供商，改后缀后同步顶层 ``model_provider``（config
    与内存）。返回提供商 id（改后缀时为新 id）。
    """
    existing = pv.load_providers().get(pid)
    if existing is None:
        return pid
    is_udf = pv.is_user_defined(pid)
    fields = []
    if is_udf:
        fields.append(FormField(
            name="id_suffix", label="id 后缀", initial=pid[len("udf-"):],
            hint="提供商 id 为 udf-<后缀>；改动会同步当前提供商配置",
            validator=_id_suffix_validator(exclude_pid=pid),
        ))
    inherited = _inherited_flag_text(pid, is_udf)
    fields.extend([
        FormField(name="name", label="显示名", initial=existing.name),
        FormField(name="base_url", label="Base URL", initial=existing.base_url),
        FormField(name="api_key", label="API Key", initial=existing.api_key,
                  password=True),
        FormField(name="models", label="模型列表",
                  initial=",".join(existing.models)),
        # 提供商级默认请求设置：模型级同名配置优先于它们；留空时回退到
        # provider_presets 预设值表（命中时），再退到模型库推导
        FormField(name="send_reasoning_content",
                  label="回传 reasoning_content",
                  initial=("" if existing.send_reasoning_content is None
                           else str(existing.send_reasoning_content).lower()),
                  placeholder=inherited,
                  hint="提供商级配置；模型配置可覆盖",
                  validator=lambda t: _parse_bool(t)[1]),
        # form_ui 是单行输入，JSON 必须压成一行回显
        FormField(name="extra_body", label="extra_body",
                  initial=(json.dumps(existing.extra_body, ensure_ascii=False)
                           if existing.extra_body else ""),
                  placeholder=_inherited_extra_body_text(pid),
                  hint="提供商级配置；模型配置可覆盖",
                  validator=_json_validator("extra_body")),
    ])
    form = form_ui(fields, title=f"编辑：{existing.name}（{pid}）",
                   style=_current_style())
    if form.aborted:
        return pid
    models = _parse_model_list(form.values.get("models", ""))
    base_url = form.values.get("base_url", "").strip()
    if not base_url or not models:
        return pid
    flag, _ = _parse_bool(form.values.get("send_reasoning_content", ""))
    existing.name = form.values.get("name", "").strip() or existing.name
    existing.base_url = base_url
    existing.api_key = form.values.get("api_key", "")
    existing.models = models
    existing.send_reasoning_content = flag
    existing.extra_body = _split_extra_body(form.values.get("extra_body", ""))
    new_id = pid
    if is_udf:
        suffix = form.values.get("id_suffix", "").strip()
        if suffix and f"udf-{suffix}" != pid:
            new_id = pv.rename_provider(pid, suffix) or pid
    existing.id = new_id
    pv.save_provider(existing)
    # 若当前提供商被改后缀（rename_provider 已同步 model_provider），
    # 或当前模型被取消勾选，重新读取当前以判断是否清空
    pid_cur, model_cur = pv.get_current() or ("", "")
    if pid_cur == new_id and model_cur not in existing.models:
        pv.set_current(new_id, "")
    return new_id


def edit_reselect_models(pid: str) -> None:
    """重选提供商勾选的模型。"""
    existing = pv.load_providers().get(pid)
    if existing is None:
        return
    infos = candidate_providers()
    if pid in infos:
        models = infos[pid].models
    else:
        # 自定义提供商无候选数据：无显示名，仅按 id 排序
        models = {m: mr.ModelInfo(id=m) for m in existing.models}
    # 回显现有勾选
    opts = _model_options(models, selected=set(existing.models))
    pick = filter_ui(opts, title=f"勾选 {existing.name}（{pid}）的模型",
                     description=f"可勾选多个（上限 {MAX_MODELS_PER_PROVIDER}）",
                     multi=True, style=_current_style())
    if pick.aborted:
        return
    existing.models = _cap_models(pick.selected)
    pv.save_provider(existing)
    # 当前模型被取消勾选：只清模型，提供商保留
    pid_cur, model_cur = pv.get_current() or ("", "")
    if pid_cur == pid and model_cur not in existing.models:
        pv.set_current(pid, "")


def delete_provider(pid: str) -> bool:
    """删除提供商（ask_ui 二次确认，默认不删）；返回是否已删除。

    确认问题「取消删除」在前：光标默认停在该项，直接 Enter / Ctrl-C
    均不删除。
    """
    existing = pv.load_providers().get(pid)
    if existing is None:
        return False
    n = len(existing.models)
    result = ask_ui([AskQuestion(
        title="确认删除模型提供商",
        description=f"是否删除模型提供商：{existing.name}（{pid} · {n} 模型）",
        options=[
            AskOption(label="取消删除", value=CONFIRM_CANCEL),
            AskOption(label="确认删除", value=CONFIRM_DELETE),
        ],
    )], style=_current_style())
    if result.aborted:
        return False
    answer = result.answers[0]
    if CONFIRM_DELETE not in answer.selected:
        return False
    pv.delete_provider(pid)
    pid_cur, _ = pv.get_current() or ("", "")
    if pid_cur == pid:
        pv.set_current("", "")
    return True


# ---------------------------------------------------------------------------
# 模型配置（显示名 / 回传 reasoning_content / extra_body）
# ---------------------------------------------------------------------------

def _is_model_configured(cfg: pv.ModelConfig | None) -> bool:
    """该模型是否已自定义过开关或 extra_body。"""
    return cfg is not None and bool(
        cfg.send_reasoning_content is not None or cfg.extra_body)


def _model_config_option(pid: str, model: str,
                         cfg: pv.ModelConfig | None) -> AskOption:
    """单个模型的菜单项：label「模型名（模型id）」，已配置则标注。"""
    name = pv.resolve_model_name(pid, model)
    return AskOption(
        label=f"{name}（{model}）",
        value=f"{MODEL_CONFIG_PREFIX}{model}",
        description="已配置" if _is_model_configured(cfg) else "",
    )


def _model_config_options(existing: pv.ProviderConfig,
                          configs: dict[str, pv.ModelConfig]) -> list[AskOption]:
    """模型配置三级菜单选项：label 为「模型名（模型id）」。

    「模型名」用已配置的显示名，其次用 models.dev 缓存里的名字，都没有
    时用模型 id。description 标注是否已自定义配置。
    """
    return [
        _model_config_option(existing.id, m, configs.get(m))
        for m in existing.models
    ] + [AskOption(label="返回", value=MODEL_CONFIG_BACK)]


def _parse_bool(text: str) -> tuple[Optional[bool], Optional[str]]:
    """文本 → 布尔；空串返回 (None, None)，非法值返回 (None, 错误串)。"""
    s = (text or "").strip().lower()
    if not s:
        return None, None
    if s == "true":
        return True, None
    if s == "false":
        return False, None
    return None, "请填 true 或 false"


def _json_validator(label: str) -> Callable[[str], Optional[str]]:
    """JSON 对象校验器：空串合法；非 JSON 或顶层非对象返回错误串。"""
    def _validate(text: str) -> Optional[str]:
        s = (text or "").strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return f"{label}不是合法 JSON"
        if not isinstance(parsed, dict):
            return f"{label}必须是 JSON 对象"
        return None
    return _validate


def _split_extra_body(text: str) -> Optional[dict]:
    """extra_body 文本 → dict（空串/非法返回 None；调用前已由校验器拦截非法）。"""
    s = (text or "").strip()
    if not s:
        return None
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _edit_model(pid: str, model: str) -> None:
    """配置单个模型：显示名 / 回传 reasoning_content / extra_body。

    回传开关的初始值取 ``providers.resolve_send_reasoning``（已显式配置的
    用配置值，否则用 models.dev 的 interleaved 推导值）。留空表示回到
    「由 models.dev 推导」——``send_reasoning_content`` 键被删除。
    """
    existing = pv.load_providers().get(pid)
    if existing is None or model not in existing.models:
        return
    cfg = pv.load_model_configs(pid).get(model) or pv.ModelConfig(id=model)
    current_flag = pv.resolve_send_reasoning(pid, model)
    # 自定义提供商在模型库里没有数据：显示名只能回退模型 id、开关推导值
    # 恒为 false
    is_udf = pv.is_user_defined(pid)
    # 开关留空时的回退来源（与 resolve_send_reasoning 同序）：提供商级配置
    # → provider_presets 预置表 → 模型库 interleaved 推导
    from mycode import provider_presets

    provider = pv.load_providers().get(pid)
    has_provider_flag = (provider is not None
                         and provider.send_reasoning_content is not None)
    preset_flag = provider_presets.lookup_config(pid).send_reasoning_content
    if has_provider_flag:
        inherited = f"true/false，留空=按提供商级配置：{'true' if current_flag else 'false'}"
    elif isinstance(preset_flag, bool):
        inherited = f"true/false，留空=按提供商预置：{'true' if preset_flag else 'false'}"
    elif is_udf:
        inherited = "true/false，留空=不回传：false"
    else:
        inherited = f"true/false，留空=按模型库默认：{'true' if current_flag else 'false'}"
    fields = [
        FormField(name="name", label="显示名",
                  initial=cfg.name,
                  placeholder=pv.resolve_model_name(pid, model),
                  hint="留空则用模型 id" if is_udf else "留空则用模型库名称"),
        FormField(name="send_reasoning_content",
                  label="回传 reasoning_content",
                  initial="",
                  placeholder=inherited,
                  hint="历史思考随历史消息发回模型",
                  validator=lambda t: _parse_bool(t)[1]),
        # form_ui 是单行输入，JSON 必须压成一行回显（多行只会显示末行）
        FormField(name="extra_body", label="extra_body",
                  initial=(json.dumps(cfg.extra_body, ensure_ascii=False)
                           if cfg.extra_body else ""),
                  placeholder=_inherited_extra_body_text(pid, model),
                  hint="JSON 对象",
                  validator=_json_validator("extra_body")),
    ]
    if cfg.send_reasoning_content is not None:
        # 显式配置过：回显当前值；未配置则留空，placeholder 提示推导值
        fields[1].initial = "true" if cfg.send_reasoning_content else "false"
    # 显示名（未配置时 resolve_model_name 回退模型库名称 / 模型 id）
    display = pv.resolve_model_name(pid, model)
    form = form_ui(
        fields,
        title=f"模型配置：{display}（{model}）",
        description="配置该模型的显示名、是否回传思考内容与额外请求体",
        style=_current_style(),
    )
    if form.aborted:
        return
    flag, _ = _parse_bool(form.values.get("send_reasoning_content", ""))
    pv.save_model_config(pid, pv.ModelConfig(
        id=model,
        name=form.values.get("name", "").strip(),
        send_reasoning_content=flag,
        extra_body=_split_extra_body(form.values.get("extra_body", "")),
    ))


def _run_model_config_loop(pid: str) -> None:
    """模型配置三级菜单循环：列出该提供商的模型，进入单个模型配置。"""
    while True:
        existing = pv.load_providers().get(pid)
        if existing is None:
            return
        if not existing.models:
            # 没有模型可配置：直接返回，不弹空菜单
            return
        q = AskQuestion(
            title=f"模型配置：{existing.name}",
            description=f"{pid} · {len(existing.models)} 模型",
            options=_model_config_options(existing, pv.load_model_configs(pid)),
        )
        result = ask_ui([q], style=_current_style())
        if result.aborted:
            return
        value = result.answers[0].selected[0] if result.answers[0].selected \
            else MODEL_CONFIG_BACK
        if value == MODEL_CONFIG_BACK:
            return
        if value.startswith(MODEL_CONFIG_PREFIX):
            _edit_model(pid, value[len(MODEL_CONFIG_PREFIX):])


def _edit_menu_question(pid: str, existing: pv.ProviderConfig) -> AskQuestion:
    """构造编辑二级菜单（models.dev 与自定义提供商选项不同）。"""
    opts = [AskOption(label="修改设定值", value=EDIT_VARS)]
    # 自定义提供商的模型列表在「修改设定值」表单中编辑，无「重选模型」
    if not pv.is_user_defined(pid):
        opts.append(AskOption(label="重选模型", value=EDIT_MODELS))
    opts.extend([
        AskOption(label="模型配置", value=EDIT_MODEL_CONFIG,
                  description=f"{len(existing.models)} 个模型可配置"),
        AskOption(label="删除模型提供商", value=EDIT_DELETE),
        AskOption(label="返回", value=EDIT_BACK),
    ])
    return AskQuestion(
        title=f"编辑：{existing.name}",
        description=f"{pid} · {len(existing.models)} 模型",
        options=opts,
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
        elif value == MAIN_ADD_USER_DEFINED:
            add_user_defined()
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
            # 修改 id 后缀时跟随新 id 继续编辑循环
            pid = edit_settings(pid)
        elif value == EDIT_MODELS:
            edit_reselect_models(pid)
        elif value == EDIT_MODEL_CONFIG:
            _run_model_config_loop(pid)
        elif value == EDIT_DELETE:
            delete_provider(pid)
            return