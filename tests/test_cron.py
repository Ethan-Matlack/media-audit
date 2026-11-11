"""Cron expressions for scheduled scans."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from media_audit.config import ConfigError, load_config
from media_audit.cron import CronError, parse_cron

T = datetime(2026, 11, 10, 22, 48)  # a Tuesday


@pytest.mark.parametrize(
    ("expr", "after", "expected"),
    [
        ("0 3 * * *", T, datetime(2026, 11, 11, 3, 0)),
        ("0 3 * * *", datetime(2026, 11, 11, 3, 0), datetime(2026, 11, 12, 3, 0)),
        ("*/15 * * * *", T, datetime(2026, 11, 10, 23, 0)),
        ("@hourly", T, datetime(2026, 11, 10, 23, 0)),
        ("@daily", T, datetime(2026, 11, 11, 0, 0)),
        ("30 2 * * sun", T, datetime(2026, 11, 15, 2, 30)),
        ("30 2 * * 7", T, datetime(2026, 11, 15, 2, 30)),
        ("0 9 * * mon-fri", datetime(2026, 11, 14, 12, 0), datetime(2026, 11, 16, 9, 0)),
        ("0 0 1 * *", T, datetime(2026, 12, 1, 0, 0)),
        ("0 0 1 jan *", T, datetime(2027, 1, 1, 0, 0)),
        ("0 4 1,15 * *", T, datetime(2026, 11, 15, 4, 0)),
        ("0 22-23/1 * * *", T, datetime(2026, 11, 10, 23, 0)),
        # Both day fields restricted: either matches (standard cron).
        ("0 0 20 * thu", T, datetime(2026, 11, 12, 0, 0)),
        ("0 0 29 2 *", T, datetime(2028, 2, 29, 0, 0)),
    ],
)
def test_next_after(expr: str, after: datetime, expected: datetime) -> None:
    assert parse_cron(expr).next_after(after) == expected


@pytest.mark.parametrize(
    "expr",
    ["", "0 3 * *", "60 * * * *", "0 24 * * *", "* * 0 * *", "*/0 * * * *", "5-1 * * * *",
     "0 0 31 2 *", "x * * * *", "0 3 * * funday"],
)  # fmt: skip
def test_invalid(expr: str) -> None:
    with pytest.raises(CronError):
        parse_cron(expr)


def test_schedule_config(tmp_path: Path) -> None:
    cfg_file = tmp_path / "c.yml"
    cfg_file.write_text("libraries: []\n")
    assert load_config(cfg_file, environ={}).scan.schedule == "0 3 * * *"
    for raw in ["off", "false", "''", "none", "null"]:
        cfg_file.write_text(f"scan:\n  schedule: {raw}\n")
        assert load_config(cfg_file, environ={}).scan.schedule is None, raw
    env = {"MEDIA_AUDIT_SCAN__SCHEDULE": "*/30 * * * *"}  # not valid YAML: kept as text
    assert load_config(cfg_file, environ=env).scan.schedule == "*/30 * * * *"
    assert (
        load_config(cfg_file, environ={"MEDIA_AUDIT_SCAN__SCHEDULE": "off"}).scan.schedule is None
    )
    with pytest.raises(ConfigError, match="5 fields"):
        load_config(cfg_file, environ={"MEDIA_AUDIT_SCAN__SCHEDULE": "daily"})
    assert load_config(cfg_file, environ={"MEDIA_AUDIT_WEB__PORT": "9000"}).web.port == 9000
    # An empty variable (cleared Unraid template field) leaves the config file in charge.
    cfg_file.write_text("scan:\n  schedule: 0 4 * * *\n")
    env = {"MEDIA_AUDIT_SCAN__SCHEDULE": ""}
    assert load_config(cfg_file, environ=env).scan.schedule == "0 4 * * *"
