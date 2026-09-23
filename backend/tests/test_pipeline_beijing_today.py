"""盘后管道的「今天」必须是北京日期, 不能用服务器本地 date.today()。

调度默认 15:35 北京时间。美西主机此时本地日历日仍是昨天: 未修复代码
today_exists 把「北京昨天的日K」当成今天已齐, 窗口右端停在昨天, 当日官方
收盘价永远拉不进来。UTC 主机在北京 00:00-08:00 有同样错位。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.jobs import daily_pipeline
from app.services import instrument_sync, kline_sync

BJ = date(2026, 3, 2)
LOCAL = date(2026, 3, 1)


class _StopError(Exception):
    """日K窗口已经记下, 后面的管道阶段与本 bug 无关。"""


def test_pipeline_daily_window_ends_on_beijing_today(monkeypatch, tmp_path) -> None:
    """数据修正/补拉路径的窗口右端必须是北京今天。

    raising=False: 未修复代码没有调用 cn_today, 钉了也不会被用到,
    仍走 date.today() → 与 2026-03-02 不等。

    本 fork 另有一层 end 封顶 (daily_end = last_completed_trading_day(), 见
    daily_pipeline 中「日K拉取的 end 边界」注释): 盘中/周末窗口右端回退到最近
    已收盘交易日, 避免扶摇 dump 未发布时退化逐股接口触发限流与完整性自愈死循环。
    收盘后 (调度默认 15:35) 该封顶返回的就是今天, 与上游语义一致。
    这里把封顶也钉到同一个假「今天」, 使断言仍校验上游的时区契约:
    若代码回退成 date.today(), 右端会是真实本地日 (≠ BJ) 而失败。
    """
    captured: list[date] = []

    def fake_batch(universe, repo, capset, start_date=None, end_date=None, on_chunk_done=None):
        captured.append(end_date.date() if hasattr(end_date, "date") else end_date)
        raise _StopError()

    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: BJ, raising=False)
    monkeypatch.setattr(
        daily_pipeline, "last_completed_trading_day", lambda now=None: BJ, raising=False
    )
    monkeypatch.setattr(instrument_sync, "sync_instruments", lambda data_dir: 0)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda capset, repo=None: ["600000.SH"])
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda table=None: None)
    monkeypatch.setattr(kline_sync, "sync_and_persist_daily_batch", fake_batch)

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: LOCAL,
    )
    capset = SimpleNamespace(has=lambda key: False)

    with pytest.raises(_StopError):
        daily_pipeline.run_now(repo, capset, override_start_date=LOCAL)  # type: ignore[arg-type]

    assert captured, "应发起日K范围拉取"
    assert captured[0] == BJ, f"窗口右端必须是北京日期 {BJ}, 实际 {captured[0]} (服务器本地 {date.today()})"
