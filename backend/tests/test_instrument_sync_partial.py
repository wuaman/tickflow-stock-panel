"""instruments 维表同步的残缺保护 (2026-10-14 上游 SZ 接口 503 事故)。

事故: tickflow free-api 对 SZ 连挂 4 次, sync_instruments 吞掉异常后仍用
SH+BJ 的 2817 行原子覆盖了 5882 行的维表 → 深市全部标的名称/涨跌停价丢失
(自选页深市股票只剩代码)。

断言: 部分交易所失败时用既有维表补齐 (且保留原 as_of), 而不是写残缺文件。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.services import instrument_sync


def _instrument(symbol: str, name: str, exchange: str, limit_up: float = 10.0) -> dict:
    return {
        "symbol": symbol,
        "name": name,
        "code": symbol.split(".")[0],
        "exchange": exchange,
        "region": "CN",
        "type": "stock",
        "ext": {"limit_up": limit_up, "limit_down": 1.0, "listing_date": "2000-01-01"},
    }


class _FakeExchanges:
    def __init__(self, by_exchange: dict[str, list[dict] | Exception]) -> None:
        self._by_exchange = by_exchange

    def get_instruments(self, exchange: str, instrument_type: str = "stock"):
        payload = self._by_exchange[exchange]
        if isinstance(payload, Exception):
            raise payload
        return payload


class _FakeClient:
    def __init__(self, by_exchange: dict[str, list[dict] | Exception]) -> None:
        self.exchanges = _FakeExchanges(by_exchange)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """绕过 provider 分支, 强制走 tickflow 直连路径。"""
    monkeypatch.setattr(instrument_sync, "_fetch_instruments_via_provider", lambda: None)
    return tmp_path


def _write_previous(out, rows: list[dict], as_of: date) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).with_columns(pl.lit(as_of).alias("as_of")).write_parquet(out)


def _out(data_dir):
    return data_dir / "instruments" / "instruments.parquet"


def test_partial_failure_keeps_previous_exchange_rows(data_dir, monkeypatch):
    """SZ 失败 → 用既有维表的 SZ 行补齐, 总数不缩水。"""
    out = _out(data_dir)
    _write_previous(
        out,
        [_instrument("600000.SH", "浦发银行", "SH"), _instrument("000001.SZ", "平安银行", "SZ")],
        date(2026, 10, 13),
    )
    monkeypatch.setattr(
        instrument_sync, "get_client",
        lambda: _FakeClient({
            "SH": [_instrument("600000.SH", "浦发银行", "SH")],
            "SZ": RuntimeError("503 Service Unavailable"),
            "BJ": [],
        }),
    )

    rows = instrument_sync.sync_instruments(data_dir)

    assert rows == 2
    df = pl.read_parquet(out)
    assert sorted(df["symbol"].to_list()) == ["000001.SZ", "600000.SH"]


def test_carried_rows_keep_their_own_as_of(data_dir, monkeypatch):
    """补齐行不盖当日日期: 否则昨日涨跌停价会被下游当成当日权威价。"""
    out = _out(data_dir)
    _write_previous(out, [_instrument("000001.SZ", "平安银行", "SZ")], date(2026, 10, 13))
    monkeypatch.setattr(
        instrument_sync, "get_client",
        lambda: _FakeClient({
            "SH": [_instrument("600000.SH", "浦发银行", "SH")],
            "SZ": RuntimeError("503 Service Unavailable"),
            "BJ": [],
        }),
    )

    instrument_sync.sync_instruments(data_dir)

    df = pl.read_parquet(out)
    as_of = {r["symbol"]: r["as_of"] for r in df.iter_rows(named=True)}
    assert as_of["000001.SZ"] == date(2026, 10, 13)  # 补齐行: 原 as_of
    assert as_of["600000.SH"] == date.today()        # 本次成功拉取的行: 当日


def test_partial_failure_without_previous_file_writes_nothing(data_dir, monkeypatch):
    """既无既有维表可补 → 放弃写入, 而不是把残缺数据落盘。"""
    monkeypatch.setattr(
        instrument_sync, "get_client",
        lambda: _FakeClient({
            "SH": [_instrument("600000.SH", "浦发银行", "SH")],
            "SZ": RuntimeError("503 Service Unavailable"),
            "BJ": [],
        }),
    )

    assert instrument_sync.sync_instruments(data_dir) == 0
    assert not _out(data_dir).exists()


def test_all_exchanges_failed_keeps_previous_file(data_dir, monkeypatch):
    """全挂 → 返回 0 且不动原文件 (调用方据此跳过缓存重建)。"""
    out = _out(data_dir)
    _write_previous(out, [_instrument("000001.SZ", "平安银行", "SZ")], date(2026, 10, 13))
    monkeypatch.setattr(
        instrument_sync, "get_client",
        lambda: _FakeClient({
            "SH": RuntimeError("503"), "SZ": RuntimeError("503"), "BJ": RuntimeError("503"),
        }),
    )

    assert instrument_sync.sync_instruments(data_dir) == 0
    df = pl.read_parquet(out)
    assert df["symbol"].to_list() == ["000001.SZ"]
    assert df["as_of"].to_list() == [date(2026, 10, 13)]


def test_healthy_sync_is_a_full_overwrite(data_dir, monkeypatch):
    """三个交易所都成功 → 正常整表覆盖 (原行为不变)。"""
    out = _out(data_dir)
    _write_previous(out, [_instrument("999999.SZ", "退市股", "SZ")], date(2026, 10, 13))
    monkeypatch.setattr(
        instrument_sync, "get_client",
        lambda: _FakeClient({
            "SH": [_instrument("600000.SH", "浦发银行", "SH")],
            "SZ": [_instrument("000001.SZ", "平安银行", "SZ")],
            "BJ": [_instrument("430047.BJ", "诺思兰德", "BJ")],
        }),
    )

    assert instrument_sync.sync_instruments(data_dir) == 3
    df = pl.read_parquet(out)
    assert sorted(df["symbol"].to_list()) == ["000001.SZ", "430047.BJ", "600000.SH"]
    assert set(df["as_of"].to_list()) == {date.today()}
