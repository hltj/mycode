"""ask_user 工具：向用户提出带选项的交互式询问。

- 预置选项（可单选 / 多选）+ 一个自定义输入选项；
- 单选可把某个选项标为「推荐」（label 加 ``（推荐）`` 后缀展示，
  返回的 value 仍是原始标签）；
- 自定义选项的标签与占位文本可用 ``custom_label`` / ``placeholder``
  指定，未指定时用默认值；
- 用户以 Ctrl-C 中止时抛出 ``AbortLoop``，由 agent_loop 分发工具结果
  事件后退出一轮 agent 循环。
"""

import json
from typing import Annotated

from mycode.ask_ui import AskOption, ask_ui as ask_ui_impl
from mycode.renderer import _get_renderer
from mycode.session import AbortLoop
from mycode.tools_registry import ToolsRegistry


# 默认自定义选项标签与占位文本（未指定 custom_label / placeholder 时使用）
_DEFAULT_CUSTOM_LABEL = "其他"
_DEFAULT_PLACEHOLDER = "输入你的回答"

# 推荐选项的「（推荐）」后缀（紧跟 label，无空格）
_RECOMMENDED_SUFFIX = "（推荐）"


def build_ask_options(
    options: list[dict] | None,
    custom_label: str,
    placeholder: str,
    multi: bool = False,
) -> list[AskOption]:
    """把入参预置选项及末尾自定义选项拼成 ask_ui 选项列表。

    - 单选（``multi=False``）时把第一个 ``recommended=True`` 的选项提到
      最前，其 label 追加 ``（推荐）`` 后缀（value 仍为原始标签）；
      即使预置选项只有一项也可推荐（末尾始终有自定义输入作为备选）；
    - 多选不调整顺序，也不加推荐后缀；
    - 末尾追加自定义输入选项（占位文本取 ``placeholder``）。
    """
    raw_options = list(options or [])
    # 过滤非法项：非 dict / 空 label 的选项跳过
    opts: list[AskOption] = [
        AskOption(label=label, value=label, description=opt.get("description"))
        for opt in raw_options
        if isinstance(opt, dict)
        and isinstance(label := opt.get("label"), str)
        and label
    ]
    # 自定义选项标签与占位固定为入参值（默认为「其他」/「输入你的回答」）
    custom = AskOption(
        label=custom_label,
        description=placeholder,
        is_custom=True,
    )
    # 预置选项为空时只有自定义选项
    if not opts:
        return [custom]

    # 单选 + 推荐选项：把第一个 recommended=True 的选项提到第一位，
    # 展示 label 追加「（推荐）」后缀；value 保持原始标签。
    # 多选不调整顺序，也不加推荐后缀。
    if not multi:
        first_rec = next(
            (i for i, o in enumerate(raw_options)
             if isinstance(o, dict) and o.get("recommended")),
            -1,
        )
        if first_rec >= 0:
            rec = opts[first_rec]
            rec.label = rec.label + _RECOMMENDED_SUFFIX
            opts = [rec] + [o for i, o in enumerate(opts) if i != first_rec]

    # 自定义选项始终排在最后
    return [*opts, custom]


@ToolsRegistry.tool(
    description=(
        "向用户展示一个询问界面让用户作答。可提供若干预置选项（可单选也可"
        "多选），并始终附带一个自定义输入选项。返回结果 JSON 文本："
        "selected 为选中项数组（单选只有一项）；自定义输入内容在 input 字段"
        "（未选中自定义输入时无该字段）。"
    )
)
def ask_user(
    title: Annotated[str, "简短标题"],
    question: Annotated[str | None, "完整问题"] = None,
    options: Annotated[
        list[dict] | None,
        "选项数组；每项含 label（必填 string）、description（可选 string）、"
        "recommended（可选 boolean，单选时可标一个推荐选项，通常推荐选项作为"
        "第一个选项提供）",
    ] = None,
    multi: Annotated[bool, "是否多选，默认否"] = False,
    custom_label: Annotated[str, "自定义回答标签，默认“其他”"] = _DEFAULT_CUSTOM_LABEL,
    placeholder: Annotated[str, "自定义回答输入占位文本“输入你的回答”"] = _DEFAULT_PLACEHOLDER,
) -> str:
    """弹出交互式询问，返回结果 JSON 文本。

    - 单选时始终会有一个自定义输入选项；
    - 预置选项 + 自定义选项拼成数组调 ask_ui；
    - 用户以 Ctrl-C 中止时抛出 ``AbortLoop``（agent_loop 捕获后分发
      工具结果事件并退出 agent 循环），不返回正常结果。
    """
    ask_options = build_ask_options(options, custom_label, placeholder, multi)
    # 与 cli 提示词输入框共用样式表（让 class:placeholder / class:mycode-input
    # 等样式类生效）
    style = _get_renderer().create_prompt_style()
    result = ask_ui_impl(
        title=title,
        description=question or "",
        options=ask_options,
        multi=multi,
        style=style,
    )
    if result.aborted:
        # 用户以 Ctrl-C 中止交互：agent_loop 捕获 AbortLoop 后分发工具
        # 结果事件（含本段文本）并退出 agent 循环
        raise AbortLoop("Error: 用户中止回答")
    payload: dict = {"selected": list(result.selected)}
    # 选中自定义选项（即使输入为空串）时带出 input 字段
    if result.input is not None:
        payload["input"] = result.input
    return json.dumps(payload, ensure_ascii=False)


__all__ = [
    "ask_user",
    "build_ask_options",
    "_DEFAULT_CUSTOM_LABEL",
    "_DEFAULT_PLACEHOLDER",
]