#!/usr/bin/env python3
"""
TDD tests for tools.py
"""
import importlib
import os

import mycode.tools as tools
from mycode.tools_registry import ToolsRegistry


def _ensure_registered():
    """确保工具已注册（模块导入时已注册，但 reset 后会清掉）

    包结构下需要 reload 子模块 ``mycode.tools.bash``，因为 reload 包本身
    不会重新执行子模块文件，装饰器不会再次触发。
    使用 ``importlib.import_module`` 拿真正的子模块（包 ``__init__`` 里
    ``from mycode.tools.bash import bash`` 会把 ``mycode.tools.bash`` 属性
    覆盖为函数，直接 import 拿不到子模块对象）。
    """
    if "bash" not in [t["function"]["name"] for t in ToolsRegistry.get_tools()]:
        import importlib as _il
        bash_mod = _il.import_module("mycode.tools.bash")
        _il.reload(bash_mod)
        # 重新触发 ``mycode.tools`` 包初始化以恢复 ``bash`` 函数属性
        _il.reload(tools)


def test_bash_tool_registered():
    """测试 bash 工具已正确注册"""
    _ensure_registered()
    tool_def = ToolsRegistry.get_tool_def("bash")
    assert tool_def is not None
    assert tool_def["function"]["name"] == "bash"
    assert tool_def["function"]["description"] == "运行 bash 命令"
    assert tool_def["type"] == "function"
    params = tool_def["function"]["parameters"]
    assert params["properties"]["command"]["type"] == "string"
    assert "command" in params["required"]


def test_bash_tool_handler_exists():
    """测试 bash 工具处理函数存在"""
    _ensure_registered()
    handler = ToolsRegistry.get_handler("bash")
    assert handler is not None
    assert callable(handler)


def test_bash_execute_simple_command():
    """测试 bash 执行简单命令"""
    result = tools.bash("echo hello")
    assert "hello" in result


def test_bash_execute_pwd():
    """测试 bash 执行 pwd 命令"""
    result = tools.bash("pwd")
    assert os.getcwd() in result


def test_bash_timeout():
    """测试 bash 超时处理"""
    saved = os.environ.get('BASH_TIMEOUT')
    os.environ['BASH_TIMEOUT'] = '1'
    result = tools.bash("sleep 10")
    assert "超时" in result
    if saved is not None:
        os.environ['BASH_TIMEOUT'] = saved
    else:
        os.environ.pop('BASH_TIMEOUT', None)


def test_bash_dangerous_command_blocked():
    """测试危险命令被阻止（BASH_DANGEROUS 视为正则）"""
    saved = os.environ.get('BASH_DANGEROUS')
    os.environ['BASH_DANGEROUS'] = 'echo_dangerous_test_12345'
    result = tools.bash("echo_dangerous_test_12345")
    assert "拒绝执行危险命令" in result
    if saved is not None:
        os.environ['BASH_DANGEROUS'] = saved
    else:
        os.environ.pop('BASH_DANGEROUS', None)


def test_bash_dangerous_multiple_patterns():
    """多条正则逗号分隔：前面的不匹不影响后面的命中"""
    saved = os.environ.get('BASH_DANGEROUS')
    os.environ['BASH_DANGEROUS'] = r'no_match_pattern_xyz,echo_dangerous_test_12345'
    result = tools.bash("echo_dangerous_test_12345")
    assert "拒绝执行危险命令" in result
    if saved is not None:
        os.environ['BASH_DANGEROUS'] = saved
    else:
        os.environ.pop('BASH_DANGEROUS', None)


def test_bash_dangerous_anchored_regex():
    """正则 ^ 锚定生效：含子串但不以它开头的命令不被拒"""
    saved = os.environ.get('BASH_DANGEROUS')
    os.environ['BASH_DANGEROUS'] = r'^dangerous_anchor_test'
    # 含子串但在中间——正则 ^dangerous_anchor_test 不匹
    result = tools.bash("echo dangerous_anchor_test")
    assert "拒绝执行危险命令" not in result
    # 以 dangerous_anchor_test 开头——拒
    result2 = tools.bash("dangerous_anchor_test xyz")
    assert "拒绝执行危险命令" in result2
    if saved is not None:
        os.environ['BASH_DANGEROUS'] = saved
    else:
        os.environ.pop('BASH_DANGEROUS', None)


def test_tools_includes_bash():
    """测试工具列表中包含 bash"""
    _ensure_registered()
    sdk_tools = ToolsRegistry.get_tools()
    tool_names = [t["function"]["name"] for t in sdk_tools]
    assert "bash" in tool_names


def test_bash_tool_description():
    """测试 bash 工具的 command 参数描述"""
    _ensure_registered()
    tool_def = ToolsRegistry.get_tool_def("bash")
    params = tool_def["function"]["parameters"]
    assert params["properties"]["command"]["description"] == "要执行的 bash 命令"


# ===================================================================
# 8 个新工具：注册检查与基础功能测试
# ===================================================================

# 这些工具共享路径检查，需要可控制的 CWD
import pytest
from pathlib import Path


def _new_tools_names() -> list[str]:
    return ["ls", "glob", "grep", "read", "write", "edit", "patch", "todo_write"]


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 CWD 切到 tmp_path，并在里面放几个示例文件。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.txt").write_text("hello world\nfoo bar baz\nhello again\n")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "deep.txt").write_text("deep\nnested\nmatch\n")
    (tmp_path / "data.json").write_text('{"k": "v"}\n')
    return tmp_path


@pytest.mark.parametrize("name", _new_tools_names())
def test_new_tool_registered(name: str):
    """每个新工具都正确注册到 ToolsRegistry。"""
    assert name in [t["function"]["name"] for t in ToolsRegistry.get_tools()]
    td = ToolsRegistry.get_tool_def(name)
    assert td is not None
    assert td["type"] == "function"
    assert "properties" in td["function"]["parameters"]


@pytest.mark.parametrize("name", _new_tools_names())
def test_new_tool_handler_exists(name: str):
    """每个新工具都有可调用的 handler。"""
    h = ToolsRegistry.get_handler(name)
    assert h is not None
    assert callable(h)


# --- ls ---

def test_ls_basic(workdir: Path):
    """自实现 ls：权限、大小、ISO-8601 日期、文件名。"""
    from mycode.tools import ls
    out = ls(".")
    assert "hello.txt" in out
    assert "nested" in out
    # 权限列：10 字符（1 类型 + 9 位 rwx/-）
    # hello.txt 是普通文件 → 首字符 '-'
    for line in out.splitlines():
        if "hello.txt" in line:
            perm = line[:10]
            assert perm[0] == "-"
            assert all(c in "rwx-" for c in perm[1:])
        if "nested" in line:
            perm = line[:10]
            assert perm[0] == "d"
    # ISO-8601 日期（带时区偏移，形如 2026-07-26T15:28:43+08:00）
    import re
    assert re.search(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)", out
    ), f"missing timezone in: {out!r}"


def test_ls_directory_suffix(workdir: Path):
    """目录条目附加 '/' 后缀。"""
    from mycode.tools import ls
    out = ls(".")
    # nested 是目录
    for line in out.splitlines():
        if "nested" in line and not "deep" in line:
            assert line.rstrip().endswith("nested/")


def test_ls_executable_suffix(workdir: Path):
    """可执行普通文件附加 '*' 后缀。"""
    from mycode.tools import ls
    (workdir / "run.sh").write_text("#!/bin/sh\n")
    (workdir / "run.sh").chmod(0o755)
    out = ls(".")
    found = False
    for line in out.splitlines():
        if "run.sh" in line:
            assert line.rstrip().endswith("run.sh*")
            # 权限位应包含 x
            assert "x" in line[:10]
            found = True
    assert found


def test_ls_symlink_suffix(workdir: Path):
    """符号链接附加 '@' 后缀。"""
    from mycode.tools import ls
    os.symlink("hello.txt", workdir / "lnk")
    out = ls(".")
    found = False
    for line in out.splitlines():
        if "lnk" in line:
            assert line.rstrip().endswith("lnk@")
            # 文件类型字符是 'l'
            assert line[0] == "l"
            found = True
    assert found


def test_ls_limit_truncates(workdir: Path):
    """limit 触发截断。"""
    from mycode.tools import ls
    # 创建多个文件
    for i in range(10):
        (workdir / f"file{i}.txt").write_text(str(i))
    out = ls(".", limit=3)
    assert "已截断" in out
    # 按名排序后前 3 个 = data.json, file0.txt, file1.txt（workdir 自带 data.json）
    assert "data.json" in out
    assert "file0.txt" in out
    assert "file1.txt" in out
    # 后面的 file5+ 一定不在输出
    assert "file5.txt" not in out
    assert "file9.txt" not in out


def test_ls_outside_cwd_blocked(workdir: Path):
    from mycode.tools import ls
    out = ls("..")
    assert out.startswith("Error:")
    assert "超出" in out


def test_ls_not_a_dir(workdir: Path):
    from mycode.tools import ls
    out = ls("hello.txt")
    assert out.startswith("Error:")


# --- glob ---

def test_glob_basic(workdir: Path):
    from mycode.tools import glob
    out = glob(".", "*.txt")
    # glob 默认 fd/find 不会返回目录
    assert "hello.txt" in out
    assert "deep.txt" in out


def test_glob_recursive(workdir: Path):
    from mycode.tools import glob
    out = glob(".", "**/*.txt")
    assert "hello.txt" in out
    assert "deep.txt" in out


def test_glob_no_match(workdir: Path):
    from mycode.tools import glob
    out = glob(".", "*.xyz")
    assert "无匹配" in out


def test_glob_limit(workdir: Path):
    from mycode.tools import glob
    out = glob(".", "*", limit=1)
    assert "已截断" in out


# --- grep ---

def test_grep_basic(workdir: Path):
    from mycode.tools import grep
    out = grep("hello", ".")
    assert "hello.txt" in out
    assert "hello world" in out


def test_grep_ignore_case(workdir: Path):
    from mycode.tools import grep
    out = grep("HELLO", ".", ignore_case=True)
    assert "hello.txt" in out


def test_grep_literal(workdir: Path):
    """literal=True 时模式中的正则元字符被字面化。"""
    from mycode.tools import grep
    # 文件中无 "foo.*baz" 字面量 → literal 模式无匹配
    out = grep("foo.*baz", ".", literal=True)
    assert "无匹配" in out
    # 同一模式关闭 literal → 当作正则，匹配 "foo bar baz"
    out2 = grep("foo.*baz", ".")
    assert "foo bar baz" in out2


def test_grep_glob_filter(workdir: Path):
    from mycode.tools import grep
    out = grep("hello", ".", glob="*.txt")
    assert "hello.txt" in out


def test_grep_no_match(workdir: Path):
    from mycode.tools import grep
    out = grep("nonexistent_xyz_pattern", ".")
    assert "无匹配" in out


def test_grep_fallback_rejects_without_no_ignore(monkeypatch, workdir: Path):
    """fallback 到 grep 且 no_ignore=False 时返回错误（grep 不支持 .gitignore）。"""
    import shutil
    orig_which = shutil.which
    def fake_which(name: str):
        if name == "rg":
            return None
        return orig_which(name)
    monkeypatch.setattr(shutil, "which", fake_which)
    from mycode.tools import grep
    out = grep("hello", ".")
    assert out.startswith("Error:")
    assert "ripgrep" in out or "rg" in out
    assert "no_ignore" in out


def test_grep_fallback_works_with_no_ignore(monkeypatch, workdir: Path):
    """fallback 到 grep 且 no_ignore=True 时正常搜索。"""
    import shutil
    orig_which = shutil.which
    def fake_which(name: str):
        if name == "rg":
            return None
        return orig_which(name)
    monkeypatch.setattr(shutil, "which", fake_which)
    from mycode.tools import grep
    out = grep("hello", ".", no_ignore=True)
    assert "hello.txt" in out
    assert "Error" not in out


def test_grep_truncate_kib(workdir: Path):
    """truncate=1 KiB 限制下大输出会被截断，且行内不被切断。"""
    from mycode.tools import grep
    long_line = "x" * 500
    (workdir / "big.txt").write_text("\n".join([long_line] * 10) + "\n")
    out = grep("xxx", "big.txt", truncate=1)
    assert "已截断" in out
    # 关键不变量：除 marker 外，所有匹配行要么完整出现，要么完全不出现
    # （"1:" + 500 个 x）形式完整行就是完整的 "xxx..."；不存在半截的
    expected_full_line = "1:" + long_line
    # 检查：要么没出现该完整行（被截断前），要么出现的该行是完整的
    full_count = out.count(expected_full_line)
    # 半截行不应存在：不会出现仅含部分 x 的 "1:" 开头行
    for line in out.split("\n"):
        if line.startswith("1:"):
            # 该行要么是完整 expected_full_line，要么不应该出现
            assert line == expected_full_line


def test_grep_limit(workdir: Path):
    from mycode.tools import grep
    # 在 4 行文件里搜，会有 2 个匹配
    out = grep("hello", ".", limit=1)
    assert "已截断" in out


def test_grep_context(workdir: Path):
    from mycode.tools import grep
    out = grep("foo", ".", context=1)
    # 上下文行应包含前后各一行
    assert "hello world" in out
    assert "hello again" in out


# --- read ---

def test_read_basic(workdir: Path):
    from mycode.tools import read
    out = read("hello.txt")
    lines = out.splitlines()
    # 带行号 → 第一列应是行号
    assert lines[0].startswith("1\t") or lines[0].startswith("1 ")
    assert "hello world" in out


def test_read_offset_and_limit(workdir: Path):
    from mycode.tools import read
    out = read("hello.txt", offset=2, limit=1)
    assert "2\t" in out
    assert "foo bar baz" in out
    assert "hello world" not in out


def test_read_offset_beyond(workdir: Path):
    from mycode.tools import read
    out = read("hello.txt", offset=100)
    assert "offset 越界" in out


def test_read_truncate_kib(workdir: Path):
    """truncate 触发截断，行内不被切断。"""
    from mycode.tools import read
    # 写一个文件：每行很长
    long = "x" * 500
    (workdir / "big.txt").write_text("\n".join([long] * 10) + "\n")
    out = read("big.txt", truncate=1)  # 1 KiB 预算
    assert "已截断" in out
    # 行内不被切断：每条 "1:\txxxx..." 要么完整要么不出现
    for line in out.split("\n"):
        if line.startswith(tuple(str(i) for i in range(1, 10))):
            # 行号开头的行必须是完整的 long 行（带行号+tab+500 个 x）
            # 不能是 "1:\txxxx..." 半截
            parts = line.split("\t", 1)
            if len(parts) == 2:
                assert parts[1] == long


def test_read_truncate_only(workdir: Path):
    """仅靠 truncate 触发截断（无 limit）。"""
    from mycode.tools import read
    # 100 行短文本 + truncate=1 KiB：行号宽度 + 内容约 10 字节/行，
    # 1 KiB ≈ 1024 字节预算，能容纳约 92 行；剩下 8 行被截断
    (workdir / "m.txt").write_text("\n".join(f"line{i}" for i in range(100)))
    out = read("m.txt", truncate=1)
    assert "已截断" in out
    assert len(out.encode("utf-8")) <= 1024
    # 后几行（行号 95+）不应出现
    for i in range(95, 100):
        assert f"line{i}" not in out


def test_read_limit_and_truncate_coexist(workdir: Path):
    """limit 切片后，truncate 仍可独立触发。"""
    from mycode.tools import read
    long_line = "x" * 500
    (workdir / "m.txt").write_text("\n".join([long_line] * 10))
    # limit=10（10 行）后 truncate=1（1 KiB 不足以装下 10 行长行）
    out = read("m.txt", limit=10, truncate=1)
    assert "已截断" in out
    # 行号开头的行：要么完整出现，要么完全不出现
    full = f"\t{long_line}"
    for line in out.split("\n"):
        if "\t" in line:
            assert line.endswith(full) or line.endswith("已截断")


def test_read_not_file(workdir: Path):
    from mycode.tools import read
    out = read("nested")
    assert out.startswith("Error:")


def test_read_outside_cwd_blocked(workdir: Path):
    from mycode.tools import read
    out = read("../escape.txt")
    assert out.startswith("Error:")


# --- write ---

def test_write_creates_file(workdir: Path):
    from mycode.tools import write
    out = write("new.txt", "abc\n")
    assert "已写入" in out
    assert (workdir / "new.txt").read_text() == "abc\n"


def test_write_creates_parent_dirs(workdir: Path):
    from mycode.tools import write
    out = write("a/b/c.txt", "deep")
    assert "已写入" in out
    assert (workdir / "a" / "b" / "c.txt").read_text() == "deep"


def test_write_overwrites(workdir: Path):
    from mycode.tools import write
    write("hello.txt", "new content")
    assert (workdir / "hello.txt").read_text() == "new content"


def test_write_outside_cwd_blocked(workdir: Path):
    from mycode.tools import write
    out = write("../leak.txt", "x")
    assert out.startswith("Error:")


# --- edit ---

def test_edit_replaces_first(workdir: Path):
    from mycode.tools import edit
    # "foo" 只出现 1 次 → 默认替换首处
    out = edit("hello.txt", "foo", "FOO", replace_all=False)
    assert "已替换 1 处" in out
    content = (workdir / "hello.txt").read_text()
    assert "FOO bar baz" in content
    assert "hello world" in content
    assert "hello again" in content


def test_edit_replace_all(workdir: Path):
    from mycode.tools import edit
    out = edit("hello.txt", "hello", "HI", replace_all=True)
    assert "已替换 2 处" in out
    content = (workdir / "hello.txt").read_text()
    assert "HI world" in content
    assert "HI again" in content


def test_edit_multiple_without_replace_all(workdir: Path):
    from mycode.tools import edit
    out = edit("hello.txt", "hello", "HI")
    assert "不唯一" in out
    assert "无法替换" in out


def test_edit_not_found(workdir: Path):
    from mycode.tools import edit
    out = edit("hello.txt", "nonexistent", "x")
    assert "未找到" in out


# --- patch ---

def test_patch_p0_style(workdir: Path):
    from mycode.tools import patch
    # 在 workdir 中已有 hello.txt；用 dir_path="." 在其父目录上应用
    diff = (
        "--- hello.txt\n"
        "+++ hello.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " hello world\n"
        "-foo bar baz\n"
        "+FOO BAR BAZ\n"
        " hello again\n"
    )
    out = patch(diff, dir_path=".")
    assert "已应用补丁" in out
    assert "FOO BAR BAZ" in (workdir / "hello.txt").read_text()


def test_patch_into_subdirectory(workdir: Path):
    """dir_path 可指向子目录，diff 中的路径按 -p1 解析。"""
    from mycode.tools import patch
    (workdir / "sub").mkdir(exist_ok=True)
    (workdir / "sub" / "target.txt").write_text("alpha\nbeta\ngamma\n")
    diff = (
        "--- a/target.txt\n"
        "+++ b/target.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " alpha\n"
        "-beta\n"
        "+BETA\n"
        " gamma\n"
    )
    out = patch(diff, dir_path="sub")
    assert "已应用补丁" in out
    assert "-p1" in out
    assert (workdir / "sub" / "target.txt").read_text() == "alpha\nBETA\ngamma\n"


def test_patch_p1_style(workdir: Path):
    from mycode.tools import patch
    diff = (
        "--- a/hello.txt\n"
        "+++ b/hello.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " hello world\n"
        "-foo bar baz\n"
        "+FOO BAR BAZ\n"
        " hello again\n"
    )
    out = patch(diff, dir_path=".")
    assert "已应用补丁" in out
    assert "-p1" in out
    assert "FOO BAR BAZ" in (workdir / "hello.txt").read_text()


def test_patch_dry_run_failure(workdir: Path):
    from mycode.tools import patch
    bad = (
        "--- a/hello.txt\n"
        "+++ b/hello.txt\n"
        "@@ -1,99 +1,2 @@\n"
        "-nope\n"
        "+x\n"
    )
    out = patch(bad, dir_path=".")
    assert "Error" in out
    # 文件不应被破坏
    assert "hello world" in (workdir / "hello.txt").read_text()


def test_patch_dir_path_outside_cwd_blocked(workdir: Path, monkeypatch):
    from mycode.tools import patch
    monkeypatch.chdir(workdir)
    out = patch("--- a/x\n+++ b/x\n", dir_path="../")
    assert out.startswith("Error:")
    assert "超出" in out


# --- todo_write ---

def test_todo_write_basic():
    from mycode.tools.todo_write import todo_write, reset_todos, get_todos
    reset_todos()
    out = todo_write([
        {"title": "第一步", "status": "completed"},
        {"title": "第二步", "status": "in_process"},
        {"title": "第三步", "status": "pending"},
    ])
    # todo_write 返回成功概要，不含渲染符号
    assert "TODO 列表已更新" in out
    assert "3 项" in out
    # 渲染符号由 cli 渲染层负责，工具只暴露原始数据
    state = get_todos()
    assert [it["title"] for it in state] == ["第一步", "第二步", "第三步"]
    assert len(state) == 3
    assert state[1]["status"] == "in_process"


def test_todo_write_replaces_state():
    """第二次调用应整体替换（不是追加）。"""
    from mycode.tools.todo_write import todo_write, reset_todos, get_todos
    reset_todos()
    todo_write([{"title": "a", "status": "pending"}])
    todo_write([{"title": "b", "status": "pending"}])
    state = get_todos()
    assert len(state) == 1
    assert state[0]["title"] == "b"


def test_todo_write_invalid_status():
    from mycode.tools.todo_write import todo_write, reset_todos
    reset_todos()
    out = todo_write([{"title": "x", "status": "bogus"}])
    assert "Error" in out


def test_todo_write_multiple_in_process():
    from mycode.tools.todo_write import todo_write, reset_todos
    reset_todos()
    out = todo_write([
        {"title": "a", "status": "in_process"},
        {"title": "b", "status": "in_process"},
    ])
    assert "Error" in out
    assert "in_process" in out


def test_todo_write_empty():
    from mycode.tools.todo_write import todo_write, reset_todos, get_todos
    reset_todos()
    out = todo_write([])
    assert "已清空" in out
    # 工具不渲染，渲染由 cli 负责；这里只验证状态被清空
    assert get_todos() == []


def _make_todo_call(call_id: str, items: list) -> object:
    """构造一个 ToolCallEvent（duck typing 即可，不依赖 session 模块）。"""
    import json
    from types import SimpleNamespace
    return SimpleNamespace(
        tool_call={
            "id": call_id,
            "type": "function",
            "function": {
                "name": "todo_write",
                "arguments": json.dumps({"items": items}),
            },
        },
    )


def test_rebuild_from_history_replays_in_order():
    """历史中的多次 todo_write 按时间顺序 replay，状态由最后一次决定。"""
    from mycode.tools.todo_write import (
        todo_write, reset_todos, get_todos, rebuild_from_history
    )
    reset_todos()
    todo_write([{"title": "临时", "status": "pending"}])  # 重建前先污染
    entries = [
        _make_todo_call("c1", [{"title": "a", "status": "completed"},
                                {"title": "b", "status": "in_process"}]),
        # 中间穿插一个其他工具的调用，不应影响
        type("Other", (), {"tool_call": {"id": "x", "type": "function",
                                          "function": {"name": "bash", "arguments": "{}"}}})(),
        _make_todo_call("c2", [{"title": "x", "status": "pending"}]),
    ]
    rebuild_from_history(entries)
    state = get_todos()
    assert len(state) == 1
    assert state[0]["title"] == "x"


def test_rebuild_from_history_skips_invalid_calls():
    """格式异常的 todo_write 调用被跳过，不影响后续 replay。"""
    from mycode.tools.todo_write import reset_todos, get_todos, rebuild_from_history
    reset_todos()
    # 构造一个 arguments 格式异常的 entry
    import json
    from types import SimpleNamespace
    bad = SimpleNamespace(tool_call={
        "id": "bad", "type": "function",
        "function": {"name": "todo_write", "arguments": "{not json"},
    })
    good = _make_todo_call("good", [{"title": "ok", "status": "pending"}])
    rebuild_from_history([bad, good])
    state = get_todos()
    assert len(state) == 1
    assert state[0]["title"] == "ok"


def test_rebuild_from_history_empty():
    """空历史或无 todo_write 调用 → 状态为空。"""
    from mycode.tools.todo_write import (
        todo_write, reset_todos, get_todos, rebuild_from_history
    )
    reset_todos()
    todo_write([{"title": "leftover", "status": "pending"}])
    rebuild_from_history([])
    assert get_todos() == []
