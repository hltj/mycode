"""
提供商预设值表。

本表承载两类预设信息，一条记录至少设置一类（可同时设置两类）：

- ``config``：预设请求设置（``send_reasoning_content`` /
  ``extra_body``）。``/provider`` 里手工配置这两项之前先查本表：提供商 id
  命中 ``for_providers`` 时采用对应 ``config`` 里的值——省去逐家提供商
  重复填写（如多家需要开启思考回传的厂商共用一条）。
- ``openai_compatible``：OpenAI 兼容端点信息（目前只有 ``path``）。
  models.dev 里标注了其他 npm（如 MiniMax 的 ``@ai-sdk/anthropic``）但
  实际提供 OpenAI 兼容 API 的提供商，``models_registry`` 用这里的
  ``path`` 替换其 ``api`` 的 URL path，把它们纳入 OpenAI 兼容候选。

用 dataclass 定义（frozen + slots），字段有类型约束且不可变；导入时把
``for_providers`` 展开成两个索引（``_CFG_BY_PROVIDER``：提供商 id → config
副本；``_OAI_BY_PROVIDER``：提供商 id → openai_compatible），``lookup_config`` /
``lookup_openai_compatible`` 直接查表。同一提供商 id 只出现在一条记录的
``for_providers`` 里。改表后需重新导入模块刷新索引。

请求设置的生效顺序（见 ``providers.resolve_send_reasoning`` /
``resolve_extra_body``）：**模型级配置 → 提供商级配置 → 本表预设值 →
models.dev 推导**。本表只作预设值，用户在 ``/provider`` 里显式配置后即
覆盖它。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True, slots=True)
class PresetConfig:
    """一条记录的预设请求设置（两项至少填一项）。"""

    send_reasoning_content: Optional[bool] = None
    extra_body: Optional[dict[str, Any]] = None


@dataclass(frozen=True, slots=True)
class OpenAICompatible:
    """一条记录的 OpenAI 兼容端点信息（目前只有 ``path``）。

    ``path`` 是该提供商 OpenAI 兼容端点的 URL path（如 ``/v1``）：
    ``models_registry`` 用它替换 models.dev ``api`` 字段的 URL path
    （scheme / netloc / query / fragment 保留），使标注了其他 npm 的
    提供商也能进入 OpenAI 兼容候选。
    """

    path: str


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """一条提供商预设值记录。

    Attributes:
        id: 记录 id（仅便于识别）。
        name: 显示名（仅便于识别）。
        for_providers: 适用的提供商 id 列表。
        config: 预设请求设置（可选）。
        openai_compatible: OpenAI 兼容端点信息（可选）。

    ``config`` 与 ``openai_compatible`` 是两类预设信息，一条记录至少
    设置一类（同时设置也合法，如 MiniMax：既预设请求设置，又给出
    OpenAI 兼容端点）。
    """

    id: str
    name: str
    for_providers: tuple[str, ...]
    config: Optional[PresetConfig] = None
    openai_compatible: Optional[OpenAICompatible] = None


# 提供商预设值
_PROVIDER_PRESETS: list[ProviderPreset] = [
    ProviderPreset(
        id="alibaba",
        name="阿里云",
        for_providers=("alibaba-cn", "alibaba", "alibaba-coding-plan-cn", "alibaba-coding-plan", "alibaba-token-plan-cn", "alibaba-token-plan"),
        config=PresetConfig(
            send_reasoning_content=True,
            extra_body={"enable_thinking": True, "preserve_thinking": True},
        ),
    ),
    ProviderPreset(
        id="deepseek",
        name="DeepSeek",
        for_providers=("deepseek",),
        config=PresetConfig(send_reasoning_content=True),
    ),
    ProviderPreset(
        id="kimi",
        name="Kimi",
        for_providers=("kimi-code-plan-cn", "kimi-code-plan-global", "moonshotai-cn", "moonshotai"),
        config=PresetConfig(
            send_reasoning_content=True,
            extra_body={"thinking": {"type": "enabled", "keep": "all"}},
        ),
    ),
    ProviderPreset(
        id="minimax",
        name="MiniMax",
        for_providers=("minimax-cn", "minimax", "minimax-cn-coding-plan", "minimax-coding-plan"),
        config=PresetConfig(send_reasoning_content=False),
        # models.dev 把四条都标成 @ai-sdk/anthropic，但 MiniMax 同时提供
        # OpenAI 兼容 API（https://api.minimax.io/v1、
        # https://api.minimax.cn/v1）
        openai_compatible=OpenAICompatible(path="/v1"),
    ),
    ProviderPreset(
        id="stepfun",
        name="阶跃星辰",
        for_providers=("stepfun", "stepfun-ai", "stepfun-step-plan", "stepfun-ai-step-plan"),
        config=PresetConfig(
            send_reasoning_content=True,
            extra_body={"reasoning_format": "deepseek-style"},
        ),
    ),
    ProviderPreset(
        id="tencent",
        name="腾讯",
        for_providers=("tencent-coding-plan", "tencent-token-plan", "tencent-tokenhub"),
        config=PresetConfig(
            send_reasoning_content=True,
            extra_body={"preserved_thinking": True},
        ),
    ),
    ProviderPreset(
        id="xiaomi",
        name="小米",
        for_providers=("xiaomi", "xiaomi-token-plan-cn", "xiaomi-token-plan-ams", "xiaomi-token-plan-sgp"),
        config=PresetConfig(send_reasoning_content=True),
    ),
    ProviderPreset(
        id="z-ai",
        name="智谱",
        for_providers=("zhipuai", "zhipuai-coding-plan", "zai", "zai-coding-plan"),
        config=PresetConfig(
            send_reasoning_content=True,
            extra_body={"thinking": {"type": "enabled", "clear_thinking": False}},
        ),
    ),
]


def _rebuild_cfg_index(table: list[ProviderPreset] | None = None
                   ) -> dict[str, PresetConfig]:
    """由表展开构建请求设置索引：提供商 id → 该条的 config 副本。

    每个提供商 id 各存一份独立副本（``extra_body`` 深拷贝），调用方改动
    不影响其他条目；未设置 ``config`` 的记录不进本索引。``table`` 缺省
    用模块里的 ``_PROVIDER_PRESETS``；测试可传入自定义表来验证本函数。
    """
    entries = _PROVIDER_PRESETS if table is None else table
    return {
        pid: PresetConfig(
            send_reasoning_content=entry.config.send_reasoning_content,
            extra_body=deepcopy(entry.config.extra_body),
        )
        for entry in entries
        for pid in entry.for_providers
        if entry.config is not None
    }


def _rebuild_oai_index(table: list[ProviderPreset] | None = None
                       ) -> dict[str, OpenAICompatible]:
    """由表展开构建 OpenAI 兼容端点索引：提供商 id → openai_compatible。

    未设置 ``openai_compatible`` 的记录不进本索引。``table`` 缺省用模块
    里的 ``_PROVIDER_PRESETS``；测试可传入自定义表来验证本函数。
    """
    entries = _PROVIDER_PRESETS if table is None else table
    return {
        pid: entry.openai_compatible
        for entry in entries
        for pid in entry.for_providers
        if entry.openai_compatible is not None
    }


# 查询索引：提供商 id → 该条的 config（导入时由上面的表展开而来）
_CFG_BY_PROVIDER: dict[str, PresetConfig] = _rebuild_cfg_index()

# 查询索引：提供商 id → 该条的 openai_compatible（frozen 只读，直接共享）
_OAI_BY_PROVIDER: dict[str, OpenAICompatible] = _rebuild_oai_index()

# 未命中时的返回：两项都未设置的空配置
_EMPTY = PresetConfig()


def lookup_config(pid: str) -> PresetConfig:
    """查某提供商的预设请求设置；未命中返回两项皆空的 ``PresetConfig``。

    返回值是 ``frozen`` 配置，字段未设置时为 ``None``。
    """
    return _CFG_BY_PROVIDER.get(pid, _EMPTY)


def lookup_openai_compatible(pid: str) -> Optional[OpenAICompatible]:
    """查某提供商的 OpenAI 兼容端点信息；未命中返回 ``None``。

    返回值是 ``frozen`` 只读对象（``path`` 为字符串），调用方可直接使用。
    """
    return _OAI_BY_PROVIDER.get(pid)
