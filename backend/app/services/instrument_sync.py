"""标的维表同步服务。

盘前 9:10 调用 tf.exchanges.get_instruments("SH"/"SZ"/"BJ", type="stock")
获取全量标的元数据，flatten ext 字段，写入 instruments.parquet。

Starter+ 盘后可用 quotes.get(universes) 顺便补充 name。
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

from app.services.fs_utils import atomic_write_parquet
from app.tickflow.client import get_client

logger = logging.getLogger(__name__)

_EXCHANGES = ["SH", "SZ", "BJ"]


def _flatten_instruments(items: list[dict]) -> list[dict]:
    """把 SDK 返回的 Instrument 列表 flatten 成扁平行。"""
    rows = []
    for item in items:
        row = {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "code": item.get("code"),
            "exchange": item.get("exchange"),
            "region": item.get("region"),
            "type": item.get("type"),
        }
        ext = item.get("ext") or {}
        row["listing_date"] = ext.get("listing_date")
        row["total_shares"] = ext.get("total_shares")
        row["float_shares"] = ext.get("float_shares")
        row["tick_size"] = ext.get("tick_size")
        row["limit_up"] = ext.get("limit_up")
        row["limit_down"] = ext.get("limit_down")
        rows.append(row)
    return rows


def _fetch_instruments_via_provider() -> list[dict] | None:
    """若当前日K数据源不是 tickflow 且该 provider 提供 get_instruments, 用它拉标的维表。

    返回 flatten 行列表; 未命中(仍应走 tickflow)时返回 None。
    标的维表跟随日K数据源(二者天然耦合, 无独立偏好项)。
    """
    from app.services import preferences

    provider_name = preferences.get_daily_data_provider()
    if provider_name == "tickflow":
        return None
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider(provider_name):
        return None
    provider = custom_sources.get_provider(provider_name)
    if not hasattr(provider, "get_instruments"):
        return None
    try:
        items = provider.get_instruments("stock") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("provider %s get_instruments 失败: %s", provider_name, e)
        return None
    rows = _flatten_instruments(items)
    logger.info("instruments via %s: %d stocks", provider_name, len(rows))
    return rows


def _align_to_schema(df: pl.DataFrame, other: pl.DataFrame) -> pl.DataFrame:
    """把 other 对齐到 df 的列顺序与 dtype (other 缺列补 null), 供 vertical concat。"""
    exprs = [
        pl.col(name).cast(dtype, strict=False) if name in other.columns
        else pl.lit(None, dtype=dtype).alias(name)
        for name, dtype in df.schema.items()
    ]
    return other.select(exprs)


def _carry_over_failed_exchanges(
    df: pl.DataFrame, failed: list[str], out: Path
) -> pl.DataFrame | None:
    """用既有维表补齐同步失败的交易所, 返回合并帧; 无既有维表可补时返回 None。

    补齐行**保留各自原 as_of** —— 下游 indicators 以 `维表 as_of == 行情日` 判定
    权威涨跌停价是否可信, 陈旧行会自然回退理论价; 若统一盖当日日期, 等于把昨日
    涨跌停价冒充当日权威价 (涨停漏判 + 假涨停混入)。
    """
    try:
        prev = pl.read_parquet(out) if out.exists() else None
    except Exception as e:  # noqa: BLE001
        logger.warning("读取既有 instruments 维表失败: %s", e)
        prev = None
    if prev is None or prev.is_empty() or "exchange" not in prev.columns:
        return None
    carried = prev.filter(pl.col("exchange").is_in(failed))
    if carried.is_empty():
        return None
    logger.warning(
        "instruments 同步不完整: %s 拉取失败, 已用既有维表 %d 行补齐 (保留原 as_of)",
        "/".join(failed), carried.height,
    )
    return pl.concat([df, _align_to_schema(df, carried)], how="vertical")


def sync_instruments(data_dir: Path) -> int:
    """全量同步标的维表 → data/instruments/instruments.parquet。

    返回写入的行数。

    任一交易所拉取失败时**不得用残缺数据覆盖维表**: 维表是名称/股本/涨跌停价的
    唯一来源, 一次上游 503 会让缺的那部分标的 (名称显示、按名称搜索、涨跌停价)
    整天失效。改为用既有维表的对应交易所行补齐; 连既有维表都没有则放弃本次写入。
    """
    all_rows = _fetch_instruments_via_provider()
    failed: list[str] = []
    if all_rows is None:
        # 未命中非 tickflow provider → 走 tickflow 直连
        tf = get_client()
        all_rows = []
        for ex in _EXCHANGES:
            try:
                items = tf.exchanges.get_instruments(ex, instrument_type="stock")
                if items:
                    all_rows.extend(_flatten_instruments(items))
                    logger.info("instruments %s: %d stocks", ex, len(items))
                else:
                    failed.append(ex)
                    logger.warning("instruments %s: 返回空", ex)
            except Exception as e:
                failed.append(ex)
                logger.warning("get_instruments(%s) failed: %s", ex, e)

    if not all_rows:
        return 0

    df = pl.DataFrame(all_rows)
    df = df.with_columns(pl.lit(date.today()).alias("as_of"))

    out = data_dir / "instruments" / "instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    if failed:
        merged = _carry_over_failed_exchanges(df, failed, out)
        if merged is None:
            logger.error(
                "instruments 同步残缺 (%s 失败) 且无既有维表可补齐 → 放弃本次写入, 保留原文件",
                "/".join(failed),
            )
            return 0
        df = merged

    atomic_write_parquet(df, out)

    logger.info("instruments synced: %d rows → %s", df.height, out)
    return df.height


def enrich_names_from_quotes(
    data_dir: Path,
    quotes_data: list[dict],
) -> int:
    """从 quotes 响应中提取 name，更新 instruments 维表（兜底补充）。

    盘后 quotes.get(universes) 返回的数据中包含 ext.name，
    用来补充 instruments 中可能缺失的 name。
    """
    if not quotes_data:
        return 0

    # 构建 symbol → name 映射
    name_map: dict[str, str] = {}
    for q in quotes_data:
        symbol = q.get("symbol", "")
        ext = q.get("ext") or {}
        name = ext.get("name") or q.get("name", "")
        if symbol and name:
            name_map[symbol] = name

    if not name_map:
        return 0

    inst_path = data_dir / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return 0

    df = pl.read_parquet(inst_path)

    # 只更新空 name 的行
    updates = pl.DataFrame({
        "symbol": list(name_map.keys()),
        "_new_name": list(name_map.values()),
    })
    df = df.join(updates, on="symbol", how="left")
    df = df.with_columns(
        pl.when(pl.col("name").is_null() | (pl.col("name") == ""))
        .then(pl.col("_new_name"))
        .otherwise(pl.col("name"))
        .alias("name"),
    ).drop("_new_name")

    atomic_write_parquet(df, inst_path)
    logger.info("instruments name enriched from quotes: %d names", len(name_map))
    return len(name_map)
