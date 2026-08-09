"""
pytest 共享配置：autouse fixture 等。

``fake_env`` 为所有测试设置最小环境变量与临时 HOME，避免污染真实环境。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def fake_env(monkeypatch, tmp_path):
    """设置最小环境变量 + 临时 HOME，避免污染真实 ~/.mycode"""
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("MODEL_NAME", "test-model")
    monkeypatch.setenv("MYCODE_HOME_DIR", str(tmp_path / ".mycode"))
    yield
