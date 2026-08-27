"""扶摇(同花顺金融数据 API)内置数据源 provider。

方法签名对齐 custom.GenericHTTPProvider(service 分流点按这套签名调用),
注入 custom loader 注册表后, 各 service 无需改动即可路由到本 provider。

当前实现数据集: realtime (A 股全市场快照, 分页) + daily (历史日K, 前复权) + adj_factor (空)。
未声明 minute / financial → provider_has_dataset 为 False, 自动回退 tickflow。

单位口径 (CONTRIBUTING §3.1, 不可凭字段名推断):
  - 扶摇 price_change_ratio_pct 为百分数数值 (1.74 = +1.74%), 本项目 realtime
    change_pct 契约为小数制 (0.0174 = 1.74%) → 此处显式 / 100。
  - realtime volume 股→手 (/100)、amount 元→万元 (/10000), 与 daily 同口径。
  - daily: volume 股→手 (/100)、amount 元→万元 (/10000), 见 _map_historical_items。
"""
from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import polars as pl

from app.plugins.fuyao import client as fuyao_client
from app.plugins.fuyao.client import FuyaoClient, FuyaoError

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 其余数据集 provider_has_dataset 返回 False → 回退 tickflow
# adj_factor: 日K直接取前复权(adjust=forward), 复权因子返回空, 避免 stocksdk 累积因子
# 被 _apply_adj_factor 再 cum_prod 导致二次复权。见 get_adj_factors 说明。
_DATASETS = ("realtime", "daily", "adj_factor")

API_KEY_ENV = "FUYAO_API_KEY"
SECRETS_FIELD = "fuyao_api_key"  # UI 配置的 Key 存 secrets.json, 优先级高于 .env


def get_api_key() -> str:
    from app import secrets_store
    return secrets_store.get_env_backed_secret(SECRETS_FIELD, API_KEY_ENV)


def availability() -> tuple[bool, str]:
    """loader 启动自检: API Key 已配置(secrets.json 或 .env)才注册为可切换数据源。不抛异常。"""
    if get_api_key():
        return True, "ok"
    return False, f"未配置 {API_KEY_ENV}(可在设置页数据源卡片中直接填写)"


def probe_api_key(api_key: str) -> tuple[bool, str]:
    """用候选 Key 实探一次快照接口(先探后存, 对齐 /tickflow-key 语义)。不落盘。"""
    client = None
    try:
        client = fuyao_client.FuyaoClient(api_key=api_key, timeout=10.0)
        client.snapshot_page(limit=1)
        return True, "ok"
    except FuyaoError as e:
        return False, f"Key 无效或网络失败: {e}"
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


@dataclass
class _FuyaoConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "fuyao"
    display_name: str = "fuyao"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(row: dict, *names: str):
    """按优先级取第一个非 None 字段。实测字段名与官方文档示例不一致, 两者兼容。"""
    for n in names:
        if row.get(n) is not None:
            return row.get(n)
    return None


def _map_snapshot_row(row: dict, fetched_ms: int) -> dict | None:
    """扶摇快照行 → 内部 realtime record。字段缺失时按依赖推导, 不伪造数据。

    实测字段(2026-08): high_price / low_price / prev_price;
    官方文档示例: highest_price / lowest_price / prev_close_price。两者都取。
    """
    symbol = row.get("thscode")
    if not symbol:
        return None
    last = _to_float(row.get("last_price"))
    prev = _to_float(_first(row, "prev_price", "prev_close_price"))

    # 百分数 (1.74 = +1.74%) → 小数制 (0.0174), 契约见模块 docstring
    pct = _to_float(row.get("price_change_ratio_pct"))
    change_pct = pct / 100.0 if pct is not None else None

    change_amount = _to_float(row.get("price_change"))
    if change_amount is None and last is not None and prev is not None:
        change_amount = last - prev
    if change_pct is None and change_amount is not None and prev not in (None, 0):
        # 与 quote_service 的推导同口径: 小数制, 不乘 100
        change_pct = change_amount / prev

    return {
        "symbol": symbol,
        "name": row.get("name"),  # 快照无名称, 由下游维表关联
        "last_price": last,
        "prev_close": prev,
        "open": _to_float(row.get("open_price")),
        "high": _to_float(_first(row, "high_price", "highest_price")),
        "low": _to_float(_first(row, "low_price", "lowest_price")),
        "volume": (_to_float(row.get("volume")) or 0.0) / 100.0,      # 股 → 手
        "amount": (_to_float(row.get("turnover")) or 0.0) / 10000.0,  # 元 → 万元
        "change_pct": change_pct,
        "change_amount": change_amount,
        "amplitude": None,      # 快照未提供, 不启发式计算
        "turnover_rate": None,  # 需股本口径 (§3.4), 交给 enriched 管道用历史股本计算
        "timestamp": fetched_ms,
        "session": None,
    }


def _map_historical_items(symbol: str, items: list[dict]) -> pl.DataFrame:
    """扶摇历史日K item[] → 内部日K DataFrame (经 normalize_daily 收口)。

    单位口径 (与 kline_daily 契约一致):
      - date_ms: 上海时区零点毫秒 → Date
      - volume: 股 → 手 (/100)
      - amount: 元 → 万元 (/10000)
    """
    if not items:
        return pl.DataFrame()
    shanghai = timezone(timedelta(hours=8))
    rows: list[dict] = []
    for it in items:
        try:
            d = datetime.fromtimestamp(int(it["date_ms"]) / 1000, tz=shanghai).date()
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        rows.append({
            "date": d,
            "open": _to_float(it.get("open_price")),
            "high": _to_float(it.get("high_price")),
            "low": _to_float(it.get("low_price")),
            "close": _to_float(it.get("close_price")),
            "vol": (_to_float(it.get("volume")) or 0.0) / 100.0,
            "amt": (_to_float(it.get("turnover")) or 0.0) / 10000.0,
        })
    if not rows:
        return pl.DataFrame()
    from app.data_providers.normalizer import normalize_daily
    return normalize_daily(pl.DataFrame(rows), default_symbol=symbol, source="fuyao")


class FuyaoProvider:
    """扶摇数据源。realtime = A 股全市场快照(quote_service 全市场模式轮询调用)。"""

    name = "fuyao"
    builtin = True

    def __init__(self) -> None:
        self.config = _FuyaoConfig()
        self._client: FuyaoClient | None = None

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _get_client(self) -> FuyaoClient:
        if self._client is None:
            self._client = fuyao_client.FuyaoClient(api_key=get_api_key())
        return self._client

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """全市场实时快照 → 内部 realtime records。失败软返回空列表(不阻断轮询)。"""
        try:
            rows, server_ts = self._get_client().snapshot_all()
        except FuyaoError as e:
            logger.warning("扶摇实时行情拉取失败: %s", e)
            return []

        # 优先用服务端时间戳(行情归属); 缺失时退回本地时间
        fetched_ms = server_ts or int(time.time() * 1000)

        records = []
        dropped = 0
        for row in rows:
            rec = _map_snapshot_row(row, fetched_ms)
            if rec is not None:
                records.append(rec)
            else:
                dropped += 1
        if dropped and not records:
            # 整页都识别不出 thscode → 大概率接口 schema 变了, 明确告警而非静默空数据
            logger.warning("扶摇快照 %d 行全部缺少 thscode 字段, 疑似接口结构变化", dropped)
            return []
        logger.info("扶摇实时行情拉取完成: %d 条(丢弃 %d 行)", len(records), dropped)
        return records

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",  # noqa: ARG002
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """A 股历史日K (前复权 adjust=forward)。接口单只一次请求, 逐只拉取拼接。

        签名对齐 stocksdk provider.get_daily (service 分流点按此调用)。
        单只失败软跳过(记 warning), 不阻断整批; 缺口可后续 repair 补拉。
        """
        if not symbols:
            return pl.DataFrame()
        end_ms = int(end_time.timestamp() * 1000) if end_time else int(time.time() * 1000)
        start_ms = int(start_time.timestamp() * 1000) if start_time else end_ms - 365 * 86400 * 1000

        frames: list[pl.DataFrame] = []
        total = len(symbols)
        for i, sym in enumerate(symbols):
            try:
                # 每次迭代重新取 client: load_all() 重建注册表会 close 掉 provider 的
                # 客户端, 持有旧引用会撞 "client has been closed"。这里兜底重建。
                items = self._get_client().get_historical(sym, start_ms, end_ms, adjust="forward")
            except (FuyaoError, RuntimeError) as e:
                logger.warning("扶摇日K拉取失败(%s): %s", sym, e)
                items = []
            df = _map_historical_items(sym, items)
            if not df.is_empty():
                frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, total)
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: str = "stock",  # noqa: ARG002
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """返回空复权因子: 日K已直接取前复权 (adjust=forward), 不再叠加本地复权。

        stocksdk 的东方财富复权因子是累积值, 而 _apply_adj_factor 会对其 cum_prod,
        累积值被再累乘 → 天文数字 → 涨跌停计算溢出。故本 provider 声明 adj_factor
        数据集但返回空, 让 compute_enriched 直接采用已前复权的 OHLC, 绕过本地复权。
        """
        return pl.DataFrame()

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset == "daily":
            sym = (symbols or ["000001.SZ"])[0]
            try:
                end_ms = int(time.time() * 1000)
                items = self._get_client().get_historical(
                    sym, end_ms - 30 * 86400 * 1000, end_ms, adjust="forward",
                )
                df = _map_historical_items(sym, items)
                return {"provider": self.name, "dataset": "daily", "rows": len(df),
                        "columns": df.columns, "preview": df.head(5).to_dicts()}
            except FuyaoError as e:
                return {"provider": self.name, "dataset": "daily", "rows": 0, "error": str(e)}
        if dataset != "realtime":
            return {"provider": self.name, "dataset": dataset, "rows": 0,
                    "error": f"扶摇插件未接入 {dataset} 数据集(自动回退 TickFlow)"}
        try:
            rows, count = self._get_client().snapshot_page(limit=5)
        except FuyaoError as e:
            return {"provider": self.name, "dataset": "realtime", "rows": 0, "error": str(e)}
        fetched_ms = int(time.time() * 1000)
        head = [r for r in (_map_snapshot_row(row, fetched_ms) for row in rows) if r][:5]
        return {
            "provider": self.name,
            "dataset": "realtime",
            "rows": count or len(head),
            "columns": list(head[0].keys()) if head else [],
            "preview": head,
        }
