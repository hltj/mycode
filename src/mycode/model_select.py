"""
模型切换流程模块。

``/model`` 命令交互：从所有已配置模型提供商中切换当前使用的模型。
设计详见 ``docs/dev/model_switch_design.md``。

对外接口：

- ``choose_model()``：运行外挂 tab（提供商）+ ask_ui 单问题（模型选择）的
  完整交互，选定后写回 ``providers.set_current``，返回
  ``(provider_id, model_id)``；取消返回 None。client 的刷新由 cli 的
  模型切换收口函数 ``_dispatch_model_change`` 统一负责，本模块不依赖 cli。
- ``_build_question``：构造单个提供商的 ask_ui 问题（测试钩子）。
"""

from __future__ import annotations

from typing import Optional

from mycode.ask_ui import AskOption, AskQuestion, ask_ui
from mycode import providers as pv


def get_current() -> tuple[str, str] | None:
    return pv.get_current()


def load_providers() -> dict:
    return pv.load_providers()


def _current_style():
    from mycode.renderer import _get_renderer
    return _get_renderer().create_prompt_style()


def _build_question(providers: dict, index: int) -> AskQuestion:
    """构造第 index 个提供商的模型选择问题。"""
    pids = sorted(providers)
    pid = pids[index]
    info = providers[pid]
    current = get_current()
    cur_pid, cur_model = current if current else ("", "")
    opts = [
        AskOption(
            label=m if (pid, m) != (cur_pid, cur_model) else f"{m}（当前）",
            value=m,
        )
        for m in info.models
    ]
    title = f"◄ {info.name} ({index + 1}/{len(pids)}) ►"
    return AskQuestion(title=title, options=opts)


def choose_model() -> Optional[tuple[str, str]]:
    """运行模型切换交互；返回选中的 (provider_id, model_id) 或 None。"""
    providers = load_providers()
    if not providers:
        print("尚未配置模型提供商，请先运行 /provider 添加。")
        return None

    pids = sorted(providers)
    index = 0
    current = get_current()
    # 初始定位到当前提供商
    if current:
        cur_pid = current[0]
        if cur_pid in pids:
            index = pids.index(cur_pid)

    cursors: dict[str, int] = {}

    while True:
        pid = pids[index]
        info = providers[pid]
        q = _build_question(providers, index)
        # 注入光标：导航退出记忆的位置；首次进入定位到当前模型
        if pid in cursors:
            q.cursor_index = cursors[pid]
        elif current and current[0] == pid and current[1] in info.models:
            q.cursor_index = info.models.index(current[1])

        def _navigate(direction: int) -> bool:
            nonlocal index
            if not pids:
                return False
            index = (index + direction) % len(pids)
            return True

        result = ask_ui([q], style=_current_style(), on_navigate=_navigate,
                        navigate_desc="切换提供商")
        if result.aborted:
            return None
        answer = result.answers[0]
        # 记录该提供商光标（导航退出时也保留位置）
        cursors[pid] = answer.cursor_index
        if answer.selected:
            model = answer.selected[0]
            pv.set_current(pid, model)
            return (pid, model)
        # 未选定（on_navigate 导航退出）：不退出循环，重建相邻提供商问题
        continue