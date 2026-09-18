"""自选页 enriched 端点的 LEFT JOIN 回归测试.

核心契约 (修复 inner-filter bug 后):
  自选列表里的每一只标的都必须出现在返回结果中, 即使它不在 enriched 缓存里
  (新股 / 冷门股 / 新用户未同步). 缺失标的的指标字段为 null, 前端渲染为 "—".

旧 bug: `df_e.filter(is_in(stock_symbols))` 以 enriched 为主表, 会把不在缓存
universe 里的自选股静默丢弃.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import polars as pl

from app.api import watchlist as wl_api
from app.services import financial_sync as fs
from app.tickflow.capabilities import Cap, CapabilitySet

# 不存在的财务目录: 让端点走「无财务数据」分支
_NO_DATA_DIR = Path("/nonexistent-tickflow-test-data")


class _FakeRepo:
    """最小化 repo mock: 只实现 watchlist_enriched 调用到的方法."""

    def __init__(self, enriched_df, enriched_date, etf_df=None, etf_date=None,
                 instruments_df=None, name_map=None, etf_set=None,
                 index_df=None, index_date=None, index_set=None, data_dir=None):
        self._enriched = enriched_df
        self._enriched_date = enriched_date
        self._etf = etf_df
        self._etf_date = etf_date
        self._index = index_df
        self._index_date = index_date
        self._instruments = instruments_df or pl.DataFrame()
        self._name_map = name_map or {}
        self._etf_set = etf_set or set()
        self._index_set = index_set or set()
        # 端点取财务列时读 repo.store.data_dir; 默认指向不存在的目录 →
        # get_financial_df 返回空 → 不 JOIN 财务列 (本文件既有断言的口径)。
        # 要覆盖财务列 JOIN 就传一个含 financials/metrics/part.parquet 的临时目录。
        self.store = SimpleNamespace(data_dir=data_dir or _NO_DATA_DIR)

    def get_enriched_latest(self):
        return self._enriched, self._enriched_date

    def get_enriched_latest_asset(self, asset):
        if asset == "etf":
            etf = self._etf if self._etf is not None else pl.DataFrame()
            return etf, self._etf_date
        if asset == "index":
            idx = self._index if self._index is not None else pl.DataFrame()
            return idx, self._index_date
        return pl.DataFrame(), None

    def get_etf_symbol_set(self):
        return self._etf_set

    def get_index_symbol_set(self):
        return self._index_set

    def get_instruments(self):
        return self._instruments

    def get_name_map(self, symbols):
        return {s: n for s, n in self._name_map.items() if s in (symbols or [])}


def _make_request(repo, *, financial: bool = True):
    """最小化 Request mock。

    端点会读 request.app.state.capabilities 做财务列权限判定 (_has_financial),
    默认授 FINANCIAL, 避免落到 _financial_is_custom() 读全局配置而结果不确定。
    """
    caps = CapabilitySet()
    if financial:
        caps.grant(Cap.FINANCIAL)
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repo=repo, capabilities=caps))
    )


def _enriched_df(symbols_data):
    """symbols_data: [(symbol, close, change_pct, amount), ...]"""
    return pl.DataFrame(
        [{"symbol": s, "close": c, "change_pct": p, "amount": a, "turnover_rate": 1.0}
         for s, c, p, a in symbols_data],
        schema_overrides={
            "close": pl.Float64, "change_pct": pl.Float64,
            "amount": pl.Float64, "turnover_rate": pl.Float64,
        },
    )


def test_watchlist_symbol_not_in_enriched_still_returned(monkeypatch):
    """核心回归: 自选里有但 enriched 缓存里没有的标的, 必须仍返回一行 (指标 null)."""
    # enriched 缓存只覆盖 600519, 不覆盖 999999 (新加的冷门股)
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": "600519"}, {"symbol": "999999"}])
    repo = _FakeRepo(
        enriched_df=_enriched_df([("600519", 1800.0, 1.2, 1e9)]),
        enriched_date="2026-07-08",
        name_map={"600519": "贵州茅台", "999999": "未知股"},
    )

    # ext_columns 显式传 None 绕过 FastAPI Query 默认值
    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)

    syms = [r["symbol"] for r in res["rows"]]
    assert "600519" in syms, "缓存里有的标的必须返回"
    assert "999999" in syms, "缓存里没有的自选标的也必须返回 (修复的核心)"

    # 缺失标的指标应为 null
    row_999 = next(r for r in res["rows"] if r["symbol"] == "999999")
    assert row_999["close"] is None, f"缺失指标应为 null, 实际: {row_999['close']}"
    assert row_999["name"] == "未知股", "name 走 get_name_map, 应正常返回"

    # 命中标的指标正常
    row_519 = next(r for r in res["rows"] if r["symbol"] == "600519")
    assert row_519["close"] == 1800.0


def test_all_watchlist_missing_from_enriched(monkeypatch):
    """股票 enriched 缓存未就绪时, 自选仍返回占位行."""
    syms = ["000001", "000002"]
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": s} for s in syms])
    repo = _FakeRepo(
        enriched_df=pl.DataFrame(schema={"symbol": pl.Utf8}),
        enriched_date=None,
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)

    assert [r["symbol"] for r in res["rows"]] == syms
    assert all(r.get("close") is None for r in res["rows"])
    assert res["as_of"] is None


def test_partial_coverage_preserves_count(monkeypatch):
    """多只自选, 部分覆盖: 返回行数必须 == 自选股票数."""
    syms = ["600519", "000001", "999888", "888999"]
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": s} for s in syms])
    repo = _FakeRepo(
        enriched_df=_enriched_df([
            ("600519", 1800.0, 1.2, 1e9),
            ("000001", 15.0, 0.3, 2e9),
        ]),
        enriched_date="2026-07-08",
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    assert len(res["rows"]) == len(syms), \
        f"返回行数应等于自选数 {len(syms)}, 实际 {len(res['rows'])}"

    returned = {r["symbol"] for r in res["rows"]}
    assert returned == set(syms)


def test_etf_not_in_enriched_still_returned(monkeypatch):
    """ETF 同样: 自选了但 ETF enriched 缓存没有的, 也应返回 (指标 null)."""
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": "510300"}, {"symbol": "599999"}])
    repo = _FakeRepo(
        enriched_df=pl.DataFrame(schema={"symbol": pl.Utf8}),  # 无股票自选
        enriched_date=None,
        etf_df=_enriched_df([("510300", 4.0, 0.5, 1e8)]),
        etf_date="2026-07-08",
        etf_set={"510300", "599999"},
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    syms = [r["symbol"] for r in res["rows"]]
    assert "510300" in syms
    assert "599999" in syms, "ETF enriched 缺失的自选标的也必须返回"

    row_missing = next(r for r in res["rows"] if r["symbol"] == "599999")
    assert row_missing["close"] is None


def test_all_etf_watchlist_missing_from_enriched(monkeypatch):
    """ETF enriched 缓存未就绪时, 自选仍返回占位行."""
    syms = ["510300", "599999"]
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": s} for s in syms])
    repo = _FakeRepo(
        enriched_df=pl.DataFrame(schema={"symbol": pl.Utf8}),
        enriched_date=None,
        etf_df=pl.DataFrame(schema={"symbol": pl.Utf8}),
        etf_date=None,
        etf_set=set(syms),
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)

    assert [r["symbol"] for r in res["rows"]] == syms
    assert all(r.get("close") is None for r in res["rows"])
    assert res["as_of"] is None


def test_mixed_watchlist_keeps_pending_stock_rows(monkeypatch):
    """股票缓存未就绪不应影响 ETF 行, 且保持自选原始顺序."""
    syms = ["510300", "000001", "510500"]
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": s} for s in syms])
    repo = _FakeRepo(
        enriched_df=pl.DataFrame(schema={"symbol": pl.Utf8}),
        enriched_date=None,
        etf_df=_enriched_df([("510300", 4.0, 0.5, 1e8), ("510500", 6.0, -0.2, 2e8)]),
        etf_date="2026-07-08",
        etf_set={"510300", "510500"},
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)

    assert [r["symbol"] for r in res["rows"]] == syms
    assert next(r for r in res["rows"] if r["symbol"] == "000001").get("close") is None
    assert next(r for r in res["rows"] if r["symbol"] == "510300")["close"] == 4.0
    assert res["as_of"] == "2026-07-08"


def test_mixed_watchlist_keeps_pending_etf_rows(monkeypatch):
    """ETF 缓存未就绪不应影响股票行, 且保持自选原始顺序."""
    syms = ["510300", "000001", "510500"]
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": s} for s in syms])
    repo = _FakeRepo(
        enriched_df=_enriched_df([("000001", 15.0, 0.3, 2e9)]),
        enriched_date="2026-07-08",
        etf_df=pl.DataFrame(schema={"symbol": pl.Utf8}),
        etf_date=None,
        etf_set={"510300", "510500"},
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)

    assert [r["symbol"] for r in res["rows"]] == syms
    assert next(r for r in res["rows"] if r["symbol"] == "000001")["close"] == 15.0
    assert all(next(r for r in res["rows"] if r["symbol"] == symbol).get("close") is None
               for symbol in ("510300", "510500"))
    assert res["as_of"] == "2026-07-08"


def test_watchlist_enriched_index_branch(monkeypatch):
    """自选含指数: 行走 index enriched, asset_type=index, 名称回填, 股票/ETF 行不受影响。

    断言 (计划 Task 4 Step 1):
      - 指数行存在, close == 3000.0, asset_type == "index", name == "上证指数"
      - 指数行 turnover_rate 为 None (列不存在或 null, 不报错)
      - 股票行 asset_type == "stock"; ETF 行 == "etf"
      - as_of == min(股票日期, etf日期, index日期)
    """
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": "600000.SH"}, {"symbol": "510300.SH"},
                                 {"symbol": "000001.SH"}])
    repo = _FakeRepo(
        enriched_df=_enriched_df([("600000.SH", 10.0, 0.3, 2e9)]),
        enriched_date="2026-07-23",
        etf_df=_enriched_df([("510300.SH", 4.0, 0.5, 1e8)]),
        etf_date="2026-07-24",
        etf_set={"510300.SH"},
        index_df=pl.DataFrame([{"symbol": "000001.SH", "close": 3000.0, "change_pct": 0.01,
                                "amount": 1e9, "ma5": 2990.0}]),
        index_date="2026-07-24",
        index_set={"000001.SH"},
        name_map={"600000.SH": "浦发银行", "510300.SH": "沪深300ETF", "000001.SH": "上证指数"},
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)

    rows = {r["symbol"]: r for r in res["rows"]}
    # 指数行
    idx = rows["000001.SH"]
    assert idx["close"] == 3000.0
    assert idx["asset_type"] == "index"
    assert idx["name"] == "上证指数"
    # 指数无换手率: 列缺失或 null, 不报错
    assert idx.get("turnover_rate") is None
    # 股票行 / ETF 行 asset_type
    assert rows["600000.SH"]["asset_type"] == "stock"
    assert rows["510300.SH"]["asset_type"] == "etf"
    # as_of == min(三类缓存日期)
    assert res["as_of"] == "2026-07-23"


def _quote_df(rows):
    """rows: [(symbol, close, prev_close, volume, open, high, low), ...]"""
    return pl.DataFrame(
        [{"symbol": s, "close": c, "prev_close": pc, "volume": v,
          "open": o, "high": h, "low": lo, "amount": 1e9, "change_pct": 1.0}
         for s, c, pc, v, o, h, lo in rows],
        schema_overrides={k: pl.Float64 for k in
                          ("close", "prev_close", "volume", "open", "high", "low",
                           "amount", "change_pct")},
    )


def test_watchlist_enriched_limit_prices_and_quote_cols(monkeypatch):
    """昨收/成交量透传; 股票行涨跌停价按板块现算 (划断后 ST=10%), ETF 行置 null。

    涨停价口径: prev_close x (1+pct) 整数分半进位 (polars_limit_price)。
      - 600519.SH 主板 10%: 1800.0 → 涨停 1980.00 / 跌停 1620.00
      - 300001.SZ 创业 20%: 10.0 → 12.00 / 8.00
      - 600029.SH 主板 ST (2026-07-08 ≥ 划断日) 10%: 10.0 → 11.00 / 9.00
    """
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": s} for s in
                                 ("600519.SH", "300001.SZ", "600029.SH", "510300.SH")])
    repo = _FakeRepo(
        enriched_df=_quote_df([
            ("600519.SH", 1900.0, 1800.0, 123456.0, 1810.0, 1950.0, 1799.0),
            ("300001.SZ", 10.5, 10.0, 88_000.0, 10.1, 10.8, 9.9),
            ("600029.SH", 10.6, 10.0, 55_000.0, 10.0, 10.9, 9.7),
        ]),
        enriched_date="2026-07-08",
        etf_df=_quote_df([("510300.SH", 4.1, 4.0, 900_000.0, 4.0, 4.2, 3.9)]),
        etf_date="2026-07-08",
        etf_set={"510300.SH"},
        name_map={
            "600519.SH": "贵州茅台", "300001.SZ": "特锐德", "600029.SH": "ST 南航",
            "510300.SH": "沪深300ETF",
        },
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    rows = {r["symbol"]: r for r in res["rows"]}

    # 昨收/成交量 透传 (此前被 _WATCHLIST_COLS 裁掉)
    row = rows["600519.SH"]
    assert row["prev_close"] == 1800.0
    assert row["volume"] == 123456.0
    assert row["open"] == 1810.0 and row["high"] == 1950.0 and row["low"] == 1799.0

    # 股票行涨跌停价
    assert rows["600519.SH"]["limit_up_price"] == 1980.0
    assert rows["600519.SH"]["limit_down_price"] == 1620.0
    assert rows["300001.SZ"]["limit_up_price"] == 12.0
    assert rows["300001.SZ"]["limit_down_price"] == 8.0
    # 主板 ST 在 2026-07-06 划断后恢复 10%
    assert rows["600029.SH"]["limit_up_price"] == 11.0
    assert rows["600029.SH"]["limit_down_price"] == 9.0

    # ETF 行: prev_close/volume 正常透传, 涨跌停价置 null
    etf = rows["510300.SH"]
    assert etf["prev_close"] == 4.0 and etf["volume"] == 900_000.0
    assert etf["limit_up_price"] is None
    assert etf["limit_down_price"] is None


def test_watchlist_enriched_limit_prices_legacy_st(monkeypatch):
    """数据日在 2026-07-06 划断之前: 主板 ST 涨跌幅为 5% 老规则, 非 ST 不受影响。"""
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": s} for s in ("600029.SH", "600519.SH")])
    repo = _FakeRepo(
        enriched_df=_quote_df([
            ("600029.SH", 10.2, 10.0, 66_000.0, 10.0, 10.4, 9.8),
            ("600519.SH", 1900.0, 1800.0, 123456.0, 1810.0, 1950.0, 1799.0),
        ]),
        enriched_date="2026-07-01",
        name_map={"600029.SH": "ST 南航", "600519.SH": "贵州茅台"},
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    rows = {r["symbol"]: r for r in res["rows"]}

    assert rows["600029.SH"]["limit_up_price"] == 10.5
    assert rows["600029.SH"]["limit_down_price"] == 9.5
    assert rows["600519.SH"]["limit_up_price"] == 1980.0


def _write_metrics(data_dir, rows):
    """写一份 metrics 财务表: rows = [(symbol, period_end, roe, eps_basic), ...]"""
    path = data_dir / "financials" / "metrics"
    path.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        [
            {"symbol": s, "period_end": pe, "roe": roe, "gross_margin": 89.0,
             "eps_basic": eps, "debt_to_asset_ratio": 21.0}
            for s, pe, roe, eps in rows
        ]
    ).write_parquet(path / "part.parquet")


def test_watchlist_enriched_joins_financial_columns_when_capable(monkeypatch, tmp_path):
    """有 FINANCIAL 能力时按最新报告期 LEFT JOIN 财务列。

    比率字段 (roe/gross_margin/debt_ratio) 存的是百分点, 端点 ÷100 转小数与
    enriched 其它比率列同口径; eps/bps 是每股金额, 不除。
    """
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": "600519.SH"}, {"symbol": "000001.SZ"}])
    _write_metrics(tmp_path, [
        ("600519.SH", "2026-03-31", 12.0, 20.0),
        ("600519.SH", "2026-06-30", 16.75, 35.57),   # 更新期必须胜出
        # 000001.SZ 无财务数据 → 财务列为 null
    ])
    repo = _FakeRepo(
        enriched_df=_quote_df([("600519.SH", 1900.0, 1800.0, 1.0, 1.0, 1.0, 1.0),
                               ("000001.SZ", 11.0, 10.0, 1.0, 1.0, 1.0, 1.0)]),
        enriched_date="2026-07-08",
        name_map={"600519.SH": "贵州茅台", "000001.SZ": "平安银行"},
        data_dir=tmp_path,
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    rows = {r["symbol"]: r for r in res["rows"]}

    assert rows["600519.SH"]["roe"] == 16.75 / 100      # 取 2026-06-30 那期
    assert rows["600519.SH"]["gross_margin"] == 0.89    # 百分点 → 小数
    assert rows["600519.SH"]["debt_ratio"] == 0.21
    assert rows["600519.SH"]["eps"] == 35.57            # 每股金额不除
    # 无财务数据的自选股仍返回该行, 财务列为 null (不因 JOIN 丢行)
    assert rows["000001.SZ"]["eps"] is None
    assert rows["000001.SZ"]["close"] == 11.0


def test_watchlist_enriched_omits_financial_columns_without_capability(monkeypatch, tmp_path):
    """无 FINANCIAL 能力且财务源非 custom 时: 不 JOIN → 财务列不在结果里。

    这是二开加的能力门禁: 未订阅 TickFlow 财务套餐的用户不该看到财务列。
    """
    monkeypatch.setattr(wl_api.watchlist, "list_symbols", lambda: [{"symbol": "600519.SH"}])
    monkeypatch.setattr(fs, "_financial_is_custom", lambda: False)
    _write_metrics(tmp_path, [("600519.SH", "2026-06-30", 16.75, 35.57)])
    repo = _FakeRepo(
        enriched_df=_quote_df([("600519.SH", 1900.0, 1800.0, 1.0, 1.0, 1.0, 1.0)]),
        enriched_date="2026-07-08",
        name_map={"600519.SH": "贵州茅台"},
        data_dir=tmp_path,   # 数据在, 但无权限 → 仍不 JOIN
    )

    res = wl_api.watchlist_enriched(_make_request(repo, financial=False), ext_columns=None)
    row = res["rows"][0]

    assert "roe" not in row and "eps" not in row
    assert row["close"] == 1900.0   # 行情列不受影响
