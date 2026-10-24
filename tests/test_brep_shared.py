"""共有演算の失敗を、各フィレットの既存の例外として扱う契約。"""
from __future__ import annotations

import pytest

from duocad import brep_geometry, brep_operations, kernel_overflow, kernel_tight


@pytest.mark.parametrize("operation", [kernel_overflow, kernel_tight])
def test_boolean_failure_is_decided_by_the_operation(monkeypatch, operation):
    """共有演算が返す未完了ビルダは、操作層で同じ Unsupported と文面になる。"""
    class Builder:
        def IsDone(self):
            return False

    calls = []
    monkeypatch.setattr(
        brep_operations, "build_boolean",
        lambda *args: calls.append(args) or Builder(),
    )
    with pytest.raises(kernel_overflow.Unsupported, match="^cut of the rounding material failed$"):
        operation._boolean("cut", "first", "second", 1e-7)
    assert calls == [("cut", "first", "second", 1e-7)]


@pytest.mark.parametrize("operation", [kernel_overflow, kernel_tight])
def test_undefined_normal_is_decided_by_the_operation(monkeypatch, operation):
    """法線が未定義でも、フィレット固有の例外が共通幾何層へ移らない。"""
    calls = []
    monkeypatch.setattr(
        brep_geometry, "outward_normal",
        lambda *args: calls.append(args) or None,
    )
    with pytest.raises(kernel_overflow.Unsupported, match="^the face has no normal at the edge$"):
        operation._outward_normal("face", "point")
    assert calls == [("face", "point")]
