"""cap_lines 的单元测试：行数 + KiB 联合截断，字节流驱动逐行扫描。"""
import pytest

from mycode.tools._truncate import cap_lines, DEFAULT_MARKER


# --- max_lines ---

def test_cap_lines_no_limit():
    body, trunc = cap_lines(["a", "b", "c"])
    assert body == "a\nb\nc"
    assert trunc is False


def test_cap_lines_max_lines_truncates():
    body, trunc = cap_lines(["a", "b", "c", "d"], max_lines=2)
    assert body == "a\nb" + DEFAULT_MARKER
    assert trunc is True


def test_cap_lines_max_lines_equal_not_truncated():
    """行数恰好等于 max_lines 时不触发截断。"""
    body, trunc = cap_lines(["a", "b"], max_lines=2)
    assert body == "a\nb"
    assert trunc is False


def test_cap_lines_max_lines_zero():
    """max_lines=0：任何非空输入都触发截断。"""
    body, trunc = cap_lines(["a"], max_lines=0)
    assert body == DEFAULT_MARKER
    assert trunc is True


# --- max_kib ---

def test_cap_lines_max_kib_no_truncation():
    body, trunc = cap_lines(["a", "bb", "ccc"], max_kib=1)
    assert body == "a\nbb\nccc"
    assert trunc is False


def test_cap_lines_max_kib_truncates():
    # 每行 1000 字节；1 KiB = 1024 - marker 字节预算很小
    lines = ["a" * 1000, "b" * 1000, "c" * 1000]
    body, trunc = cap_lines(lines, max_kib=1)
    assert trunc is True
    # 第一行 1000 字节 < 1017 (1024 - 7) → 接受
    # 第二行 +sep = 1001，used = 2001 > 1017 → break
    assert body.startswith("a" * 1000)
    assert "b" * 1000 not in body
    assert body.endswith(DEFAULT_MARKER)


def test_cap_lines_max_kib_marker_in_budget():
    """marker 字节被预留：body 长度不应让 body+marker 超过 max_kib。"""
    lines = ["a" * 10] * 200  # 200 行 * (10 + 1) ≈ 2200 字节
    body, trunc = cap_lines(lines, max_kib=1)  # 1024 字节
    assert trunc is True
    total = body.encode("utf-8")
    # 实际 body 字节 + marker 字节 <= max_kib * 1024
    assert len(total) <= 1024


def test_cap_lines_no_truncation_when_under_budget():
    body, trunc = cap_lines(["a", "bb"], max_lines=10, max_kib=10)
    assert body == "a\nbb"
    assert trunc is False


# --- 红线优先级 ---

def test_cap_lines_both_red_lines_consistent():
    """同时设 max_lines 和 max_kib：谁先触发谁生效。"""
    body, trunc = cap_lines(["x"] * 100, max_lines=5, max_kib=100)
    # 5 行 * (1 + 1) - 1 = 9 字节（首行无 sep），远小于 100 字节
    # 所以 max_lines 先触发
    assert trunc is True
    # 只能确定 ≤ 5 行（不含 marker 行）
    actual_lines = [l for l in body.split("\n") if l and l != DEFAULT_MARKER.lstrip("\n")]
    assert len(actual_lines) <= 5


# --- 行完整性 ---

def test_cap_lines_never_splits_a_line():
    """任何一行要么完整保留，要么完全不出现，绝不在中间截断。"""
    line = "x" * 5000
    body, trunc = cap_lines([line, "tail"], max_kib=1)  # 1 KiB 不足以装下
    assert trunc is True
    # 第一行作为"超大单行"会完整保留（设计如此）
    assert "x" * 5000 in body
    # tail 不应出现
    assert "tail" not in body


def test_cap_lines_preserves_chinese():
    """utf-8 多字节字符按字节预算正确截断，不切到字符中间。"""
    # 每个汉字 utf-8 编码 3 字节；预算 ≈ 1017 字节
    lines = ["中" * 339 + "A", "B"]  # 339*3 + 1 = 1018 字节
    body, trunc = cap_lines(lines, max_kib=1)
    assert trunc is True
    # 第一行 1018 字节（含 'A'）大于预算 1017 → 单行超大特殊处理：完整保留
    assert "中" * 339 + "A" in body
    assert "B" not in body


# --- Iterable 接收 ---

def test_cap_lines_accepts_generator():
    """可迭代对象（生成器）也能用，截断后立即停止消费。"""
    def gen():
        for i in range(1000):
            yield f"line-{i}"
    body, trunc = cap_lines(gen(), max_lines=3)
    assert trunc is True
    assert body == "line-0\nline-1\nline-2" + DEFAULT_MARKER


