"""MediaInfoAnalyzer's deeper-parse retry, with MediaInfo and the child process faked."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from media_audit.analyzer import AnalyzerUnavailable
from media_audit.analyzer import mediainfo as mi_mod
from media_audit.rules.derive import derive
from tests.conftest import load_mediainfo

SHALLOW = load_mediainfo("real_bluray_hdr10_truehd_atmos")
DEEP = load_mediainfo("real_bluray_hdr10_truehd_atmos_speed005")


class _FakeMediaInfo:
    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc
        self.speeds: list[float] = []

    def parse(self, path: str, *, output: str, parse_speed: float, full: bool) -> str:
        self.speeds.append(parse_speed)
        return json.dumps(self.doc)


def _analyzer(doc: dict[str, Any], **kw: Any) -> mi_mod.MediaInfoAnalyzer:
    try:
        a = mi_mod.MediaInfoAnalyzer(**kw)
    except AnalyzerUnavailable as exc:
        pytest.skip(str(exc))
    a._mi = _FakeMediaInfo(doc)
    return a


def _fake_run(result: Any) -> Any:
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> Any:
        calls.append(cmd)
        if isinstance(result, BaseException):
            raise result
        return result

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _atmos(doc: dict[str, Any]) -> tuple[str | None, bool]:
    a = derive("x.mkv", "mkv", 1, doc).audio[0]
    return a.commercial_name, a.is_object_audio


def test_retry_success_uses_deeper_result(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = subprocess.CompletedProcess([], 0, json.dumps(DEEP).encode(), b"")
    run = _fake_run(ok)
    monkeypatch.setattr(mi_mod.subprocess, "run", run)
    doc = _analyzer(SHALLOW, retry_parse_speed=0.05, retry_timeout=30).analyze("/m/thor.mkv")
    assert _atmos(doc) == ("TrueHD Atmos", True)
    assert doc["media_audit"]["parse_speed"] == 0.05
    assert doc["media_audit"]["retry"]["status"] == "ok"
    assert run.calls[0][-2:] == ["/m/thor.mkv", "0.05"]


def test_retry_timeout_keeps_shallow_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mi_mod.subprocess, "run", _fake_run(subprocess.TimeoutExpired("x", 30)))
    doc = _analyzer(SHALLOW).analyze("/m/thor.mkv")
    assert _atmos(doc) == ("TrueHD (Atmos?)", False)
    assert doc["media_audit"] == {
        "parse_speed": 0.0,
        "retry": {"parse_speed": 0.05, "status": "timeout", "seconds": pytest.approx(0, abs=1)},
    }


@pytest.mark.parametrize(
    "proc",
    [subprocess.CompletedProcess([], 1, b"", b"boom"),
     subprocess.CompletedProcess([], 0, b"not json", b""),
     subprocess.CompletedProcess([], 0, b"{}", b"")],
)  # fmt: skip
def test_retry_error_keeps_shallow_result(
    monkeypatch: pytest.MonkeyPatch, proc: subprocess.CompletedProcess[bytes]
) -> None:
    monkeypatch.setattr(mi_mod.subprocess, "run", _fake_run(proc))
    doc = _analyzer(SHALLOW).analyze("/m/thor.mkv")
    assert _atmos(doc) == ("TrueHD (Atmos?)", False)
    assert doc["media_audit"]["retry"]["status"] == "error"


def test_no_retry_when_complete_or_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _fake_run(AssertionError("must not run"))
    monkeypatch.setattr(mi_mod.subprocess, "run", run)
    complete = load_mediainfo("real_remux_dv_hdr10_truehd_atmos")
    assert "retry" not in _analyzer(complete).analyze("/m/a.mkv")["media_audit"]
    assert "retry" not in _analyzer(SHALLOW, retry_timeout=0).analyze("/m/b.mkv")["media_audit"]
    assert run.calls == []
