"""ask_user 工具：向用户提出带选项的交互式询问。

- 一次询问 1-4 个问题（``questions`` 数组），每个问题独立作答；
- 每个问题支持预置选项（可单选 / 多选）+ 一个自定义输入选项；
- 单选可把某个选项标为「推荐」（label 加 ``（推荐）`` 后缀展示，
  返回的 value 仍是原始标签）；
- 自定义选项的标签与占位文本可用 ``custom_label`` / ``placeholder``
  指定，未指定时用默认值；
- 用户以 Ctrl-C 中止时抛出 ``AbortLoop``，由 agent_loop 分发工具结果
  事件后退出一轮 agent 循环。
"""

import json
from dataclasses import dataclass
from typing import Annotated

from mycode.ask_ui import AskOption, AskQuestion, ask_ui as ask_ui_impl
from mycode.renderer import _get_renderer
from mycode.session import AbortLoop
from mycode.tools_registry import ToolsRegistry


# 默认自定义选项标签与占位文本（未指定 custom_label / placeholder 时使用）
_DEFAULT_CUSTOM_LABEL = "其他"
_DEFAULT_PLACEHOLDER = "输入你的回答"

# 推荐选项的「（推荐）」后缀（紧跟 label，无空格）
_RECOMMENDED_SUFFIX = "（推荐）"

# 一次询问的问题数量上限
_MAX_QUESTIONS = 4


@dataclass
class AskOptionParam:
    """预置选项。

    - ``label`` 必填；``description`` / ``recommended`` 可选（有默认值）。
    """

    label: Annotated[str, "选项标签（必填）"]
    description: Annotated[str | None, "选项描述"] = None
    recommended: Annotated[bool, "是否推荐（单选时生效）"] = False


@dataclass
class AskQuestionParam:
    """单个问题。

    - ``header`` 必填；其余字段（question / options / multi /
      custom_label / placeholder）均可选（有默认值）。
    - ``options`` 未提供或为空时，问题仍附带默认的自定义输入选项。
    """

    header: Annotated[str, "问题简短标题（必填）"]
    question: Annotated[str, "完整问题"] = ""
    options: Annotated[list[AskOptionParam] | None, "预置选项数组"] = None
    multi: Annotated[bool, "是否多选，默认否"] = False
    custom_label: Annotated[str, "自定义回答标签，默认“其他”"] = _DEFAULT_CUSTOM_LABEL
    placeholder: Annotated[str, "自定义回答输入占位文本，默认“输入你的回答”"] = _DEFAULT_PLACEHOLDER


class _QuestionError(ValueError):
    """单个问题入参非法；message 即返回给模型的错误文本。"""


def build_ask_options(
    options: list[AskOptionParam] | None,
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


def build_ask_question(q: AskQuestionParam, index: int) -> AskQuestion:
    """把单个问题入参（``questions[i]``）校验并转换成 ``AskQuestion``。

    入参非法时抛 ``_QuestionError``（message 即友好错误文本）。
    """
    # 运行时防御：schema 虽约束了结构，但模型可能传非 dict / 非法 header
    if not isinstance(q, dict):
        raise _QuestionError(f"Error: questions[{index}] 必须是对象")
    header = q.get("header")
    if not isinstance(header, str) or not header:
        raise _QuestionError(
            f"Error: questions[{index}] 缺少必填字段 header（非空字符串）")
    opts_raw = q.get("options")
    if opts_raw is not None and not isinstance(opts_raw, list):
        raise _QuestionError(f"Error: questions[{index}].options 必须是数组")
    return AskQuestion(
        title=header,
        description=q.get("question") or "",
        options=build_ask_options(
            opts_raw,
            q.get("custom_label") or _DEFAULT_CUSTOM_LABEL,
            q.get("placeholder") or _DEFAULT_PLACEHOLDER,
            bool(q.get("multi")),
        ),
        multi=bool(q.get("multi")),
    )


@ToolsRegistry.tool(
    description=(
        "向用户展示一个询问界面让用户作答，一次可询问 1-4 个问题，每个问题"
        "独立作答。每个问题可提供若干预置选项（可单选也可多选），并始终附带"
        "一个自定义输入选项。返回结果 JSON 文本：answers 为答案数组（顺序与"
        "questions 一致），每项含 header、selected（选中项数组，单选只有一项）；"
        "自定义输入内容在 input 字段（未选中自定义输入时无该字段）；未作答的"
        "问题带 skipped=true。"
    )
)
def ask_user(
    questions: Annotated[list[AskQuestionParam], "问题数组（1-4 个问题）"],
) -> str:
    """弹出交互式询问，返回结果 JSON 文本。

    - 每个问题单独构建选项（单选推荐选项前置 + 自定义输入在后）；
    - 一次调用可问 1-4 个问题，全部进入同一问询界面（多问题模式）；
    - 用户以 Ctrl-C 中止时抛出 ``AbortLoop``（agent_loop 捕获后分发
      工具结果事件并退出 agent 循环），不返回正常结果。
    """
    if not questions:
        return "Error: questions 至少需要 1 个问题"
    if len(questions) > _MAX_QUESTIONS:
        return f"Error: questions 最多支持 {_MAX_QUESTIONS} 个问题"

    try:
        ask_questions = [
            build_ask_question(q, i) for i, q in enumerate(questions)
        ]
    except _QuestionError as e:
        return str(e)

    # 与 cli 提示词输入框共用样式表（让 class:placeholder / class:mycode-input
    # 等样式类生效）
    style = _get_renderer().create_prompt_style()
    result = ask_ui_impl(ask_questions, style=style)
    if result.aborted:
        # 用户以 Ctrl-C 中止交互：agent_loop 捕获 AbortLoop 后分发工具
        # 结果事件（含本段文本）并退出 agent 循环
        raise AbortLoop("Error: 用户中止回答")

    answers = [{
        "header": q.title,
        "selected": list(a.selected),
        **({"input": a.input} if a.input is not None else {}),
        **({"skipped": True} if a.skipped else {}),
    } for q, a in zip(ask_questions, result.answers)]
    return json.dumps({"answers": answers}, ensure_ascii=False)


__all__ = [
    "ask_user",
    "build_ask_options",
    "build_ask_question",
    "_DEFAULT_CUSTOM_LABEL",
    "_DEFAULT_PLACEHOLDER",
]
