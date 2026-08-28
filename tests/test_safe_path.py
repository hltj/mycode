"""safe_path 模块的单元测试。"""
import os
from pathlib import Path

import pytest

from mycode.tools._safe_path import safe_path


def _patch_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把 CWD 切到 tmp_path 并 chdir 回来（test 结束时自动还原）。"""
    monkeypatch.chdir(tmp_path)


def test_safe_path_returns_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _patch_cwd(tmp_path, monkeypatch)
    result = safe_path(".")
    assert os.path.isabs(result.abs)
    assert result.abs == os.path.realpath(tmp_path)
    assert result.wksp == os.path.realpath(tmp_path)
    assert result.rel == "."


def test_safe_path_resolves_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _patch_cwd(tmp_path, monkeypatch)
    (tmp_path / "foo").mkdir()
    result = safe_path("foo")
    assert result.abs == os.path.realpath(tmp_path / "foo")
    assert result.wksp == os.path.realpath(tmp_path)
    assert result.rel == "foo"


def test_safe_path_rel_for_nested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _patch_cwd(tmp_path, monkeypatch)
    (tmp_path / "a" / "b").mkdir(parents=True)
    result = safe_path("a/b")
    assert result.rel == os.path.join("a", "b")
    assert result.abs == os.path.realpath(tmp_path / "a" / "b")


def test_safe_path_abs_input_gets_rel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """绝对路径入参（在 CWD 内）也归一为相对工作区的 rel。"""
    _patch_cwd(tmp_path, monkeypatch)
    (tmp_path / "foo").mkdir()
    result = safe_path(str(tmp_path / "foo"))
    assert result.rel == "foo"
    assert result.abs == os.path.realpath(tmp_path / "foo")


def test_safe_path_rejects_dotdot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _patch_cwd(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="超出当前工作目录"):
        safe_path("..")


def test_safe_path_rejects_dotdot_inside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _patch_cwd(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="超出当前工作目录"):
        safe_path("a/../../escape")


def test_safe_path_rejects_absolute_outside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _patch_cwd(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="超出当前工作目录"):
        safe_path("/etc/passwd")


def test_safe_path_rejects_empty():
    with pytest.raises(ValueError, match="路径不能为空"):
        safe_path("")


def test_safe_path_protected_pattern_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _patch_cwd(tmp_path, monkeypatch)
    monkeypatch.setenv("MYCODE_PROTECTED_PATH_PATTERN", r"\.env$")
    (tmp_path / ".env").write_text("SECRET=1")
    with pytest.raises(ValueError, match=r"拒绝访问"):
        safe_path(".env")


def test_safe_path_protected_no_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _patch_cwd(tmp_path, monkeypatch)
    monkeypatch.setenv("MYCODE_PROTECTED_PATH_PATTERN", r"forbidden")
    result = safe_path("ok.txt")
    assert result.abs == os.path.realpath(tmp_path / "ok.txt")
    assert result.rel == "ok.txt"


def test_safe_path_protected_multiple_patterns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _patch_cwd(tmp_path, monkeypatch)
    monkeypatch.setenv("MYCODE_PROTECTED_PATH_PATTERN", "  foo , bar\\.txt  ,  ")
    (tmp_path / "bar.txt").write_text("x")
    with pytest.raises(ValueError, match=r"拒绝访问"):
        safe_path("bar.txt")


def test_safe_path_follows_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """软链接跟随到真实路径后再做 CWD 检查：若链指向 CWD 外应被阻止。"""
    _patch_cwd(tmp_path, monkeypatch)
    outside = tmp_path.parent / f"outside-{os.getpid()}"
    outside.mkdir()
    try:
        link = tmp_path / "leak"
        os.symlink(outside / "secret", link)
        with pytest.raises(ValueError, match="超出当前工作目录"):
            safe_path("leak")
    finally:
        outside.rmdir()


def test_safe_path_empty_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _patch_cwd(tmp_path, monkeypatch)
    monkeypatch.setenv("MYCODE_PROTECTED_PATH_PATTERN", "")
    result = safe_path(".")
    assert result.abs == os.path.realpath(tmp_path)
    assert result.rel == "."
