"""
提供商级请求设置的预设值表。

``/provider`` 里手工配置 ``send_reasoning_content`` / ``extra_body`` 之前，
先查本表：某个提供商 id 命中 ``for_providers`` 时，采用对应 ``config``
里的值——省去逐家提供商重复填写（如多家需要开启思考回传的厂商共用一条）。

用 dataclass 定义（frozen + slots），字段有类型约束且不可变；导入时把
``for_providers`` 展开成“提供商 id → config 副本”索引（``_BY_PROVIDER``），
``lookup`` 直接查表并返回 ``PresetConfig``。同一提供商 id 只出现在一条
记录的 ``for_providers`` 里。改表后需重新导入模块刷新索引。

生效顺序（见 ``providers.resolve_send_reasoning`` /
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
class ProviderPreset:
    """一条提供商预设值记录。

    Attributes:
        id: 记录 id（仅便于识别）。
        name: 显示名（仅便于识别）。
        for_providers: 适用的提供商 id 列表。
        config: 预设请求设置。
    """

    id: str
    name: str
    for_providers: tuple[str, ...]
    config: PresetConfig

    def copy_config(self) -> PresetConfig:
        """配置的独立副本（``extra_body`` 深拷贝）。

        frozen 只防属性赋值，``extra_body`` 内的 dict 仍可变；索引里给每个
        提供商 id 各存一份副本，调用方改动不影响其他条目。
        """
        return PresetConfig(
            send_reasoning_content=self.config.send_reasoning_content,
            extra_body=deepcopy(self.config.extra_body),
        )


# 提供商级请求设置的预设值
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
        config=PresetConfig(send_reasoning_content=True),
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


def _rebuild_index(table: list[ProviderPreset] | None = None
                   ) -> dict[str, PresetConfig]:
    """由表展开构建索引：提供商 id → 该条的 config 副本。

    ``table`` 缺省用模块里的 ``_PROVIDER_PRESETS``；测试可传入自定义表来
    验证本函数。
    """
    entries = _PROVIDER_PRESETS if table is None else table
    return {pid: entry.copy_config()
            for entry in entries
            for pid in entry.for_providers}


# 查询索引：提供商 id → 该条的 config（导入时由上面的表展开而来）
_BY_PROVIDER: dict[str, PresetConfig] = _rebuild_index()

# 未命中时的返回：两项都未设置的空配置
_EMPTY = PresetConfig()


def lookup(pid: str) -> PresetConfig:
    """查某提供商的预设配置；未命中返回两项皆空的 ``PresetConfig``。

    返回值是 ``frozen`` 配置，字段未设置时为 ``None``。
    """
    return _BY_PROVIDER.get(pid, _EMPTY)
