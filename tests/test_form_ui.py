"""
通用表单界面（mycode.form_ui）测试。

覆盖：

- ``FormField`` 数据与默认值
- 单字段：直接 Enter 提交；输入字符进入 Buffer
- 多字段：Enter 逐字段前进；末字段 Enter 提交全部
- 字段切换：Up/Down / C-p / C-n（含环形循环）
- 密码式掩码：password 字段显示掩码（布局层面控件为 password=True）
- 必填校验：required 空值阻止提交并提示
- 自定义校验器：返回错误字符串阻止提交、返回 None 通过
- 提交键：Alt+Enter（ESC Enter 序列）任意位置直接提交
- 取消控制：Ctrl-C 终止交互（aborted=True，values 为空）
- 占位文本：空值时展示 placeholder
- 状态持久化：focus_index 入参与返回
- 布局：标题 / label 行 / hint / 提示行
"""

from __future__ import annotations

import pytest

from mycode.form_ui import FormField, FormResult, form_ui


def _run_with_keys(seq: str, fields, *, title="", description="", focus_index=0):
    """用注入的按键序列运行 form_ui，返回 ``FormResult``。"""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    with create_pipe_input() as inp:
        inp.send_text(seq)
        return form_ui(
            fields,
            title=title,
            description=description,
            focus_index=focus_index,
            input=inp,
            output=DummyOutput(),
        )


# ===================================================================
# FormField 数据
# ===================================================================

class TestFormField:
    def test_defaults(self):
        f = FormField(name="api_key", label="API Key")
        assert f.name == "api_key"
        assert f.label == "API Key"
        assert f.initial == ""
        assert f.placeholder == ""
        assert f.password is False
        assert f.required is False
        assert f.hint == ""
        assert f.validator is None

    def test_full_kwargs(self):
        def _v(s):
            return None if s else "不能为空"

        f = FormField(
            name="u", label="U", initial="x", placeholder="输入",
            password=True, required=True, hint="必填", validator=_v,
        )
        assert f.initial == "x"
        assert f.placeholder == "输入"
        assert f.password is True
        assert f.required is True
        assert f.hint == "必填"
        assert f.validator is _v


# ===================================================================
# 单字段提交
# ===================================================================

class TestSingleField:
    def test_enter_submit_empty(self):
        """单字段直接 Enter：提交空值。"""
        r = _run_with_keys("\r", [FormField(name="a", label="A")])
        assert r.aborted is False
        assert r.values == {"a": ""}

    def test_type_then_submit(self):
        """输入字符后 Enter 提交输入值。"""
        r = _run_with_keys("hi\r", [FormField(name="a", label="A")])
        assert r.values == {"a": "hi"}

    def test_initial_value(self):
        """initial 回填，直接提交得到初始值。"""
        r = _run_with_keys("\r", [FormField(name="a", label="A", initial="old")])
        assert r.values == {"a": "old"}

    def test_alt_enter_submit(self):
        """Alt+Enter 任意位置直接提交。"""
        r = _run_with_keys("x\x1b\r", [FormField(name="a", label="A")])
        assert r.aborted is False
        assert r.values == {"a": "x"}


# ===================================================================
# 多字段切换与提交
# ===================================================================

class TestMultiField:
    def test_enter_advances_then_submits(self):
        """Enter 逐字段前进，末字段 Enter 提交全部。"""
        fields = [
            FormField(name="a", label="A"),
            FormField(name="b", label="B"),
        ]
        r = _run_with_keys("x\rz\r", fields)
        assert r.aborted is False
        assert r.values == {"a": "x", "b": "z"}

    def test_down_up_navigation(self):
        """Down/Up 切换字段；字符进入当前焦点字段。"""
        fields = [
            FormField(name="a", label="A"),
            FormField(name="b", label="B"),
        ]
        # Down(\x1b[B) 到 b 输入 z，Up(\x1b[A) 回 a 输入 x，Alt+Enter 提交
        r = _run_with_keys("\x1b[Bz\x1b[Ax\x1b\r", fields)
        assert r.values == {"a": "x", "b": "z"}

    def test_navigation_cycle(self):
        """Down/C-n 到底循环回第一项；Up/C-p 在第一项循环到末项。"""
        fields = [
            FormField(name="a", label="A"),
            FormField(name="b", label="B"),
        ]
        # Down Down：从 a → b → a；在 a 输入 x 后 Alt+Enter
        r = _run_with_keys("\x1b[B\x1b[Bx\x1b\r", fields)
        assert r.values["a"] == "x"
        # C-n C-n 同样循环
        r_c = _run_with_keys("\x0e\x0ex\x1b\r", fields)
        assert r_c.values["a"] == "x"
        # Up：从 a 循环到 b；在 b 输入 y 后 Alt+Enter
        r2 = _run_with_keys("\x1b[Ay\x1b\r", fields)
        assert r2.values["b"] == "y"
        # C-p 同样循环
        r2_c = _run_with_keys("\x10y\x1b\r", fields)
        assert r2_c.values["b"] == "y"

    def test_c_n_c_p_navigation(self):
        """C-n / C-p 切换字段（与 Down / Up 等价的绑定）。"""
        fields = [
            FormField(name="a", label="A"),
            FormField(name="b", label="B"),
        ]
        # C-n 到 b → C-p 回 a → 输入 y 落在 a
        r = _run_with_keys("\x0e\x10y\x1b\r", fields)
        assert r.values == {"a": "y", "b": ""}

    def test_alt_enter_submits(self):
        """Alt+Enter 任意位置直接提交。"""
        fields = [
            FormField(name="a", label="A"),
            FormField(name="b", label="B"),
        ]
        # 在第一个字段输入 x 后 Alt+Enter 直接提交（跳过 b）
        r = _run_with_keys("x\x1b\r", fields)
        assert r.aborted is False
        assert r.values == {"a": "x", "b": ""}


# ===================================================================
# 校验
# ===================================================================

class TestValidation:
    def test_required_blocks_empty(self):
        """required 空值：Enter 提交被阻止（仍在界面），补输入后可提交。"""
        fields = [
            FormField(name="a", label="A", required=True),
        ]
        # 先 Enter（空值被阻止），再输入 x 后 Alt+Enter
        r = _run_with_keys("\rx\x1b\r", fields)
        assert r.aborted is False
        assert r.values == {"a": "x"}

    def test_required_error_focus(self):
        """required 空值提交：焦点跳到出错字段。"""
        fields = [
            FormField(name="a", label="A"),
            FormField(name="b", label="B", required=True),
        ]
        # a 输入 x Enter 到 b，直接 Enter 提交（b 空 → 阻止），输入 y Alt+Enter
        r = _run_with_keys("x\r\ry\x1b\r", fields)
        assert r.values == {"a": "x", "b": "y"}
        assert r.focus_index == 1

    def test_validator_error_message(self):
        """自定义校验器返回错误字符串：阻止提交。"""
        def _must_be_upper(s: str):
            return None if s.isupper() else "必须全大写"

        fields = [FormField(name="a", label="A", validator=_must_be_upper)]
        # 输入小写 abc，Alt+Enter 提交被阻止；改写（backspace×3 + ABC）后提交
        r = _run_with_keys("abc\x1b\r\x7f\x7f\x7fABC\x1b\r", fields)
        assert r.aborted is False
        assert r.values == {"a": "ABC"}

    def test_validator_pass(self):
        """校验器返回 None 时正常提交。"""
        def _ok(s: str):
            return None

        fields = [FormField(name="a", label="A", validator=_ok)]
        r = _run_with_keys("v\x1b\r", fields)
        assert r.values == {"a": "v"}


# ===================================================================
# 取消控制
# ===================================================================

class TestCancel:
    def test_ctrl_c_aborts(self):
        r = _run_with_keys("x\x03", [FormField(name="a", label="A")])
        assert r.aborted is True
        assert r.values == {}


# ===================================================================
# 布局 / 样式
# ===================================================================

class TestLayout:
    def test_password_control_masked(self):
        """password 字段的输入控件带 PasswordProcessor 掩码。"""
        from mycode.form_ui import _build_field_row, _FormState
        from prompt_toolkit.layout.processors import PasswordProcessor
        state = _FormState([FormField(name="k", label="K", password=True)])
        row = _build_field_row(state, 0)
        # 行是 VSplit([label_win, input_win])
        from prompt_toolkit.layout.containers import VSplit, Window
        assert isinstance(row, VSplit)
        label_win, input_win = row.children
        assert isinstance(input_win, Window)
        procs = input_win.content.input_processors
        assert any(type(p).__name__ == "PasswordProcessor" for p in procs)

    def test_plain_control_not_masked(self):
        """普通字段不带掩码处理器。"""
        from mycode.form_ui import _build_field_row, _FormState
        from prompt_toolkit.layout.processors import PasswordProcessor
        state = _FormState([FormField(name="k", label="K")])
        row = _build_field_row(state, 0)
        procs = row.children[1].content.input_processors
        assert not any(type(p).__name__ == "PasswordProcessor" for p in procs)

    def test_title_row_style(self):
        """标题行使用 ask-title 样式类。"""
        from mycode.form_ui import _build_form_layout, _FormState
        from prompt_toolkit.layout.containers import HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        state = _FormState([FormField(name="a", label="A")], title="表单")
        layout = _build_form_layout(state)
        assert isinstance(layout, HSplit)
        first = layout.children[0]
        assert isinstance(first, Window)
        ctrl = first.content
        assert isinstance(ctrl, FormattedTextControl)
        frags = list(ctrl.text if isinstance(ctrl.text, list) else [])
        assert any("ask-title" in s for s, _ in frags)

    def test_hint_rendered(self):
        """hint 非空时 label 行包含 hint 文本（暗灰样式）。"""
        from mycode.form_ui import _build_field_row, _FormState
        state = _FormState([FormField(name="a", label="A", hint="必填")])
        row = _build_field_row(state, 0)
        ctrl = row.children[0].content
        frags = ctrl.text
        texts = [t for _, t in frags]
        assert any("必填" in t for t in texts)

    def test_placeholder_processor(self):
        """空值字段展示 placeholder（AfterInput + Condition）。"""
        from mycode.form_ui import _build_field_row, _FormState
        state = _FormState(
            [FormField(name="a", label="A", placeholder="请输入")]
        )
        row = _build_field_row(state, 0)
        bc = row.children[1].content
        procs = bc.input_processors
        assert any(type(p).__name__ == "ConditionalProcessor" for p in procs)

    def test_no_title_no_title_row(self):
        """无标题与描述时不渲染 header 行（首 child 即第一个字段行）。"""
        from mycode.form_ui import _build_form_layout, _FormState
        from prompt_toolkit.layout.containers import HSplit, Window, VSplit
        state = _FormState([FormField(name="a", label="A")], title="")
        layout = _build_form_layout(state)
        first = layout.children[0]
        # 无标题/描述：首 child 直接是 VSplit 字段行（无空行分隔）
        assert isinstance(first, VSplit)

    def test_description_rendered(self):
        """标题与描述都存在：二者之间空一行，header 后再空一行接字段行。"""
        from mycode.form_ui import _build_form_layout, _FormState
        from prompt_toolkit.layout.containers import HSplit, Window, VSplit
        state = _FormState(
            [FormField(name="a", label="A")], title="表单", description="说明文字"
        )
        layout = _build_form_layout(state)
        children = layout.children
        # children: 标题行 / 空行 / 描述行 / 空行 / 字段行 ...
        assert isinstance(children[0], Window)   # 标题
        assert isinstance(children[1], Window)   # 标题与描述间空行
        assert isinstance(children[2], Window)   # 描述
        assert isinstance(children[3], Window)   # header 与字段区间空行
        assert isinstance(children[4], VSplit)   # 第一个字段行

    def test_description_without_title(self):
        """只有描述无标题：无标题行与中间空行，直接描述 + 空行 + 字段行。"""
        from mycode.form_ui import _build_form_layout, _FormState
        from prompt_toolkit.layout.containers import Window, VSplit
        state = _FormState(
            [FormField(name="a", label="A")], description="说明文字"
        )
        layout = _build_form_layout(state)
        children = layout.children
        assert isinstance(children[0], Window)   # 描述
        assert isinstance(children[1], Window)   # header 与字段区间空行
        assert isinstance(children[2], VSplit)   # 第一个字段行

    def test_field_rows_separated_by_blank(self):
        """相邻字段行之间有空行 child（输入区不连成块）。"""
        from mycode.form_ui import _build_form_layout, _FormState
        from prompt_toolkit.layout.containers import Window, VSplit
        state = _FormState([
            FormField(name="a", label="A"),
            FormField(name="b", label="B"),
        ], title="T")
        layout = _build_form_layout(state)
        children = layout.children
        # header：标题 1 + 空行 1（无描述）→ 字段 a 在下标 2，空行在 3，b 在 4
        assert isinstance(children[2], VSplit)
        assert isinstance(children[3], Window)   # 字段间空行
        assert isinstance(children[4], VSplit)
        # 字段区之后：空行 / 错误行 / 提示行（children[5:] 共 3 个）
        assert len(children) == 8


# ===================================================================
# 状态持久化
# ===================================================================

class TestStatePersistence:
    def test_focus_index_restore(self):
        """focus_index 入参：初始焦点在指定字段。"""
        fields = [
            FormField(name="a", label="A"),
            FormField(name="b", label="B"),
        ]
        # 初始焦点 b（index=1），输入 y 后 Alt+Enter
        r = _run_with_keys("y\x1b\r", fields, focus_index=1)
        assert r.values == {"a": "", "b": "y"}

    def test_focus_index_returned(self):
        """返回的 focus_index 为提交时焦点字段索引。"""
        fields = [
            FormField(name="a", label="A"),
            FormField(name="b", label="B"),
        ]
        # a Enter → b，输入 y 后 Alt+Enter 提交：焦点在 b
        r = _run_with_keys("\ry\x1b\r", fields)
        assert r.focus_index == 1

    def test_buffers_shared_between_calls(self):
        """同字段列表多次调用：Buffer 复用，输入内容跨调用保留。"""
        from mycode.form_ui import _FormState
        fields = [
            FormField(name="a", label="A", initial="keep"),
            FormField(name="b", label="B"),
        ]
        s1 = _FormState(fields)
        s1.buffers[0].insert_text("x")
        # 用同一组 Buffer 重建 state（模拟调用方回传 buffers）
        fields2 = [
            FormField(name="a", label="A", buffer=s1.buffers[0]),
            FormField(name="b", label="B", buffer=s1.buffers[1]),
        ]
        s2 = _FormState(fields2)
        assert s2.buffers[0].text == "keepx"
