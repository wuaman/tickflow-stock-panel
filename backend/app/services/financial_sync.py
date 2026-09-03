"""财务数据独立同步服务。

解耦于 K-line 管道, 自有调度 + 自有存储。
能力门控: Cap.FINANCIAL (Expert 套餐)
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from app.tickflow.capabilities import Cap, CapabilitySet

logger = logging.getLogger(__name__)

# 每个 API 请求最多 100 个标的
_BATCH_SIZE = 100

# 财务报表 + 历史股本表
FINANCIAL_TABLES = ("metrics", "income", "balance_sheet", "cash_flow", "shares")


# ================================================================
# 同步函数
# ================================================================

def _get_symbols(data_dir: Path) -> list[str]:
    """从 instruments 表获取标的列表。"""
    inst_path = data_dir / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return []
    try:
        df = pl.read_parquet(inst_path, columns=["symbol"])
        return df["symbol"].to_list()
    except Exception as e:
        logger.warning("读取 instruments 失败: %s", e)
        return []


def _get_watchlist_symbols() -> list[str]:
    """从自选股表获取标的列表 (仅 symbol)。

    用于「只同步自选股财报」——非全市场拉取, 规避上游 (东方财富等) 限流/封 IP。
    """
    try:
        from app.services import watchlist
        rows = watchlist.list_symbols()
        return [str(r["symbol"]).strip().upper() for r in rows if r.get("symbol")]
    except Exception as e:  # noqa: BLE001
        logger.warning("读取自选股失败: %s", e)
        return []


def _resolve_symbols(data_dir: Path, scope: str | None) -> tuple[list[str], str]:
    """按 scope 解析要同步的标的列表, 返回 (symbols, 实际生效的 scope)。

    scope:
      None / "all"      → 全市场 instruments
      "watchlist"       → 仅自选股; 自选股为空时返回空列表 (不回退全量, 避免误触发全市场拉取)
    """
    scope = (scope or "all").lower()
    if scope == "watchlist":
        syms = _get_watchlist_symbols()
        if not syms:
            logger.warning("scope=watchlist 但自选股为空, 跳过同步 (不回退全量)")
            return [], "watchlist"
        return syms, "watchlist"
    return _get_symbols(data_dir), "all"


def _financial_is_custom() -> bool:
    """当前财务数据源是否走 custom (用于绕过 TickFlow Expert 套餐门槛)。"""
    from app.services import preferences
    return _provider_is_custom(preferences.get_financial_provider())


def _provider_is_custom(provider: str) -> bool:
    """指定数据源是否为配置了 financial 数据集的 custom 源。"""
    if provider == "tickflow":
        return False
    from app.data_providers import custom as custom_sources
    return custom_sources.provider_has_dataset(provider, "financial")


def _fetch_table(
    table: str,
    symbols: list[str],
    capset: CapabilitySet,
    latest_only: bool = True,
    provider: str | None = None,
) -> pl.DataFrame:
    """通过财务数据源拉取一张标准化财务表。

    provider: None(默认, 用当前 preferences 里的财务源) | "fuyao" | custom 源名。
    显式指定时绕过 preferences, 供双源互补补数使用 (如 fuyao 为主源时
    定时用东财 custom 源补 watchlist 的独有字段), 不改动用户的选择。
    """
    is_custom = _financial_is_custom() if provider is None else _provider_is_custom(provider)
    if not is_custom and not capset.has(Cap.FINANCIAL):
        logger.info("sync_%s skipped: no FINANCIAL capability", table)
        return pl.DataFrame()
    if not symbols:
        logger.warning("sync_%s skipped: no symbols", table)
        return pl.DataFrame()

    # 自定义数据源分流
    if is_custom:
        from app.services import preferences
        from app.data_providers import custom as custom_sources
        source_name = provider or preferences.get_financial_provider()
        try:
            df = custom_sources.get_provider(source_name).get_financials(
                table, symbols, latest_only=latest_only
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("sync_%s custom provider failed: %s", table, e)
            return pl.DataFrame()
        if df.is_empty() or "symbol" not in df.columns:
            return pl.DataFrame()
        return df

    from app.tickflow.client import get_client
    tf = get_client()

    # 分批拉取
    api_method = {
        "metrics": tf.financials.metrics,
        "income": tf.financials.income,
        "balance_sheet": tf.financials.balance_sheet,
        "cash_flow": tf.financials.cash_flow,
        "shares": getattr(tf.financials, "shares", None),
    }[table]
    if api_method is None:
        logger.warning("sync_shares skipped: current TickFlow SDK does not support shares")
        return pl.DataFrame()

    all_records: list[dict] = []
    total_batches = (len(symbols) + _BATCH_SIZE - 1) // _BATCH_SIZE

    for i in range(0, len(symbols), _BATCH_SIZE):
        chunk = symbols[i : i + _BATCH_SIZE]
        batch_num = i // _BATCH_SIZE + 1
        try:
            data = api_method(chunk, latest=latest_only)
            # data 格式: { "600519.SH": [record, ...], ... }
            if isinstance(data, dict):
                for sym, records in data.items():
                    if isinstance(records, list):
                        for rec in records:
                            if isinstance(rec, dict):
                                rec["symbol"] = sym
                                all_records.append(rec)
            logger.debug("sync_%s batch %d/%d: %d records", table, batch_num, total_batches, len(data) if isinstance(data, dict) else 0)
        except Exception as e:
            logger.warning("sync_%s batch %d/%d failed: %s", table, batch_num, total_batches, e)

    if not all_records:
        return pl.DataFrame()

    df = pl.DataFrame(all_records)
    if df.is_empty() or "symbol" not in df.columns:
        return pl.DataFrame()
    return df


def _write_table(table: str, df: pl.DataFrame, data_dir: Path) -> int:
    if df.is_empty() or "symbol" not in df.columns:
        return 0

    # 写入 Parquet (全量覆盖)
    out_dir = data_dir / "financials" / table
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "part.parquet"
    df.write_parquet(out_file)

    logger.info("sync_%s done: %d records written", table, len(df))
    return len(df)


def _sync_table(
    table: str,
    symbols: list[str],
    data_dir: Path,
    capset: CapabilitySet,
    latest_only: bool = True,
    provider: str | None = None,
) -> int:
    """同步单张财务表。返回写入的行数。"""
    return _write_table(
        table,
        _fetch_table(table, symbols, capset, latest_only=latest_only, provider=provider),
        data_dir,
    )


def _normalize_period_columns(df: pl.DataFrame) -> pl.DataFrame:
    """归一化 period_end / announce_date 为 YYYY-MM-DD 纯日期字符串。

    不同数据源的日期口径不一: 东方财富 datacenter 返回 "2026-06-30 00:00:00"
    (带时间), 扶摇返回 "2026-06-30" (纯日期)。若不归一, 按 (symbol, period_end)
    字符串精确相等去重时同一报告期会被当成两期, 两行并存且字段各缺一半。
    统一截断到前 10 字符 (ISO 日期), 无法解析的行保持原值不丢数据。
    """
    if df.is_empty():
        return df
    exprs = []
    for col in ("period_end", "announce_date"):
        if col in df.columns and df.schema[col] == pl.Utf8:
            exprs.append(
                pl.when(pl.col(col).str.len_chars() >= 10)
                .then(pl.col(col).str.slice(0, 10))
                .otherwise(pl.col(col))
                .alias(col)
            )
    return df.with_columns(exprs) if exprs else df


def _merge_report_history(*frames: pl.DataFrame) -> pl.DataFrame:
    """按 (symbol, period_end) 合并各报告期, 同期多行逐列取最新非空值。

    语义(区分"覆盖"与"填空"): 每列独立取 announce_date 最新的非空值 —
    新同步行有值则覆盖旧值, 新行缺的列(如 fuyao 不提供的字段)由旧行补齐,
    实现多数据源并集共存。历史报告期不可变, 合并不会引入过期数据。
    无 announce_date 的帧按输入顺序, 后写优先(与旧行为 keep="last" 一致)。
    """
    valid = [
        _normalize_period_columns(frame)
        for frame in frames
        if not frame.is_empty() and {"symbol", "period_end"} <= set(frame.columns)
    ]
    if not valid:
        return pl.DataFrame()
    merged = (
        pl.concat(valid, how="diagonal_relaxed")
        .filter(pl.col("symbol").is_not_null() & pl.col("period_end").is_not_null())
    )
    sort_keys = ["symbol", "period_end"] + (
        ["announce_date"] if "announce_date" in merged.columns else []
    )
    merged = merged.sort(sort_keys, nulls_last=True)
    value_cols = [c for c in merged.columns if c not in ("symbol", "period_end")]
    return (
        merged.group_by("symbol", "period_end")
        .agg([pl.col(c).drop_nulls().last() for c in value_cols])
        .sort(["symbol", "period_end"])
    )


def _sync_history_table_for_symbols(
    table: str,
    symbols: list[str],
    data_dir: Path,
    capset: CapabilitySet,
    provider: str | None = None,
) -> int:
    """历史累积同步: 保留已有各期记录, 仅拉最新期 + 为新标的补全量历史。

    provider 透传给 _fetch_table (None=当前 preferences 的财务源)。

    与 shares 同一模式。若改为 latest_only 全量覆盖, 历史各期会在每次同步时
    被冲掉, 财务因子将永远只有单期快照, 任何回测都是未来函数。
    """
    existing = get_financial_df(data_dir, table)
    if existing.is_empty() or not {"symbol", "period_end"} <= set(existing.columns):
        return _sync_table(table, symbols, data_dir, capset, latest_only=False, provider=provider)

    existing_symbols = set(existing["symbol"].drop_nulls().to_list())
    missing_symbols = [symbol for symbol in symbols if symbol not in existing_symbols]
    missing_history = (
        _fetch_table(table, missing_symbols, capset, latest_only=False, provider=provider)
        if missing_symbols
        else pl.DataFrame()
    )
    current_symbols = [symbol for symbol in symbols if symbol in existing_symbols]
    latest = _fetch_table(table, current_symbols, capset, latest_only=True, provider=provider)
    merged = _merge_report_history(existing, missing_history, latest)
    return _write_table(table, merged, data_dir)


def sync_metrics(data_dir: Path, capset: CapabilitySet, scope: str = "all") -> int:
    """同步核心财务指标 (metrics), 历史各期累积保留。scope=watchlist 仅同步自选股。"""
    symbols, _ = _resolve_symbols(data_dir, scope)
    return _sync_history_table_for_symbols("metrics", symbols, data_dir, capset)


def sync_income(data_dir: Path, capset: CapabilitySet, scope: str = "all") -> int:
    """同步利润表, 历史各期累积保留。scope=watchlist 仅同步自选股。"""
    symbols, _ = _resolve_symbols(data_dir, scope)
    return _sync_history_table_for_symbols("income", symbols, data_dir, capset)


def sync_balance_sheet(data_dir: Path, capset: CapabilitySet, scope: str = "all") -> int:
    """同步资产负债表, 历史各期累积保留。scope=watchlist 仅同步自选股。"""
    symbols, _ = _resolve_symbols(data_dir, scope)
    return _sync_history_table_for_symbols("balance_sheet", symbols, data_dir, capset)


def sync_cash_flow(data_dir: Path, capset: CapabilitySet, scope: str = "all") -> int:
    """同步现金流量表, 历史各期累积保留。scope=watchlist 仅同步自选股。"""
    symbols, _ = _resolve_symbols(data_dir, scope)
    return _sync_history_table_for_symbols("cash_flow", symbols, data_dir, capset)


def sync_shares(data_dir: Path, capset: CapabilitySet, scope: str = "all") -> int:
    """同步历史股本表。scope=watchlist 仅同步自选股。"""
    symbols, _ = _resolve_symbols(data_dir, scope)
    return _sync_history_table_for_symbols("shares", symbols, data_dir, capset)


# 东财补充源: 仅对 watchlist 拉取, 每天 1 次增量, 避免全市场拉取封 IP。
# fuyao 缺的东财独有字段(资产负债率/存货/固定资产等)由此补齐, 主源仍是用户选的。
EM_SUPPLEMENT_PROVIDER = "eastmoney_financial"
_EM_SUPPLEMENT_HOUR = 16  # 每天本地 16:0x 触发 (A股盘后)


_FUYAO_DEEP_PREFS_KEY = "financials_fuyao_deep_symbols"


def _fuyao_deep_done() -> set[str]:
    """已做过 fuyao 全量历史补数的股票集合 (preferences 持久化, 重启不丢)。"""
    from app.services import preferences
    return set(preferences.load().get(_FUYAO_DEEP_PREFS_KEY) or [])


def _mark_fuyao_deep_done(symbols: list[str]) -> None:
    from app.services import preferences
    done = _fuyao_deep_done() | set(symbols)
    preferences.save({_FUYAO_DEEP_PREFS_KEY: sorted(done)})


def sync_watchlist_supplement(data_dir: Path, capset: CapabilitySet) -> dict[str, int]:
    """自选股双源补数 (每天盘后由调度器跑一次):

    1. 东财源: 未覆盖的自选股拉全量历史(约 50 期), 已覆盖的仅刷最新期 —
       补东财独有字段(资产负债率/存货/固定资产等)。
    2. fuyao 源: 首次进入自选的股票补一次全量历史(quarterly 20 + annual 20,
       约 40 期) — 补 fuyao 独有字段(研发费用/资本开支/流动资产合计等)。
       成功过的股票记入 preferences, 不重复拉 (fuyao 深窗口一次即拉满,
       新期由常规同步增量覆盖)。

    同一报告期两源都有数据时, _merge_report_history 逐列取最新非空 → 双源字段并存。
    前提: 主财务源不是东财 (否则与常规同步重复), 且东财 custom 源已配置可用。
    """
    from app.services import preferences

    main_provider = preferences.get_financial_provider()
    if main_provider == EM_SUPPLEMENT_PROVIDER:
        return {}  # 主源就是东财, 无需补充
    if not _provider_is_custom(EM_SUPPLEMENT_PROVIDER):
        logger.info("watchlist_supplement skipped: eastmoney_financial 源未配置")
        return {}

    symbols = _get_watchlist_symbols()
    if not symbols:
        logger.info("watchlist_supplement skipped: watchlist 为空")
        return {}

    results: dict[str, int] = {}

    # ---- 1. 东财补数 ----
    # 覆盖判定: income 表该股任一期有东财特征字段 name (SECURITY_NAME_ABBR,
    # fuyao 不产此列) → 已覆盖。
    existing_income = get_financial_df(data_dir, "income")
    em_covered: set[str] = set()
    if not existing_income.is_empty() and "name" in existing_income.columns:
        em_covered = set(
            existing_income.filter(pl.col("name").is_not_null())["symbol"]
            .unique().to_list()
        )
    deep_symbols = [s for s in symbols if s not in em_covered]
    incr_symbols = [s for s in symbols if s in em_covered]
    if deep_symbols:
        logger.info(
            "watchlist_supplement: %d 只新自选股拉东财全量历史: %s",
            len(deep_symbols), deep_symbols,
        )

    logger.info(
        "watchlist_supplement: %d 只自选股, 东财全量 %d / 增量 %d",
        len(symbols), len(deep_symbols), len(incr_symbols),
    )
    for table in FINANCIAL_TABLES:
        try:
            frames: list[pl.DataFrame] = []
            if incr_symbols:
                frames.append(_fetch_table(
                    table, incr_symbols, capset, latest_only=True,
                    provider=EM_SUPPLEMENT_PROVIDER,
                ))
            if deep_symbols:
                frames.append(_fetch_table(
                    table, deep_symbols, capset, latest_only=False,
                    provider=EM_SUPPLEMENT_PROVIDER,
                ))
            frames = [f for f in frames if not f.is_empty()]
            if not frames:
                results[table] = 0
                continue
            existing = get_financial_df(data_dir, table)
            if existing.is_empty() or not {"symbol", "period_end"} <= set(existing.columns):
                merged = _merge_report_history(*frames)
            else:
                merged = _merge_report_history(existing, *frames)
            results[table] = _write_table(table, merged, data_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning("watchlist_supplement em %s failed: %s", table, e)
            results[table] = -1

    # ---- 2. fuyao 深历史补数 (每只股票一次性) ----
    fy_pending = [s for s in symbols if s not in _fuyao_deep_done()]
    if fy_pending:
        logger.info(
            "watchlist_supplement: %d 只自选股补 fuyao 全量历史: %s",
            len(fy_pending), fy_pending,
        )
        fy_fetched: set[str] = set()
        for table in ("metrics", "income", "balance_sheet", "cash_flow"):
            try:
                fy_full = _fetch_table(table, fy_pending, capset, latest_only=False)
                if fy_full.is_empty():
                    continue
                fy_fetched.update(fy_full["symbol"].unique().to_list())
                existing = get_financial_df(data_dir, table)
                if existing.is_empty() or not {"symbol", "period_end"} <= set(existing.columns):
                    merged = _merge_report_history(fy_full)
                else:
                    merged = _merge_report_history(existing, fy_full)
                _write_table(table, merged, data_dir)
            except Exception as e:  # noqa: BLE001
                logger.warning("watchlist_supplement fuyao_deep %s failed: %s", table, e)
        # 只记成功拉到的股票; 因 429 等失败的下轮重试
        if fy_fetched:
            _mark_fuyao_deep_done(sorted(fy_fetched))
            logger.info("watchlist_supplement: fuyao 深历史完成 %d 只", len(fy_fetched))

    _refresh_financials_views(data_dir)
    logger.info("watchlist_supplement done: %s", results)
    return results


def sync_all(data_dir: Path, capset: CapabilitySet, scope: str = "all") -> dict[str, int]:
    if not capset.has(Cap.FINANCIAL) and not _financial_is_custom():
        logger.info("sync_all financials skipped: no FINANCIAL capability")
        return {}

    symbols, scope = _resolve_symbols(data_dir, scope)
    if not symbols:
        logger.info("sync_all skipped: no symbols (scope=%s)", scope)
        return {}
    results: dict[str, int] = {}
    for table in FINANCIAL_TABLES:
        results[table] = _sync_history_table_for_symbols(
            table, symbols, data_dir, capset
        )

    # 同步完成后注册 DuckDB 视图
    _refresh_financials_views(data_dir)

    return results


# ================================================================
# DuckDB 视图
# ================================================================

def _refresh_financials_views(data_dir: Path) -> None:
    """刷新财务表 DuckDB 视图 (在 DataStore.db 上注册)。"""
    d = data_dir.as_posix()
    views = {
        "financials_metrics": f"{d}/financials/metrics/*.parquet",
        "financials_income": f"{d}/financials/income/*.parquet",
        "financials_balance_sheet": f"{d}/financials/balance_sheet/*.parquet",
        "financials_cash_flow": f"{d}/financials/cash_flow/*.parquet",
        "financials_shares": f"{d}/financials/shares/*.parquet",
    }
    for name, path in views.items():
        out = data_dir / "financials" / name.replace("financials_", "") / "part.parquet"
        if not out.exists():
            continue
        # 视图注册需要由 DataStore 完成,这里只做日志
        logger.debug("financial parquet ready: %s (%d rows)", name, out.stat().st_size)


def get_financial_df(data_dir: Path, table: str) -> pl.DataFrame:
    """读取本地财务 Parquet。"""
    path = data_dir / "financials" / table / "part.parquet"
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except Exception as e:
        logger.warning("读取 financials/%s 失败: %s", table, e)
        return pl.DataFrame()


# ================================================================
# 调度器
# ================================================================

class FinancialScheduler:
    """独立调度器: 每周同步 metrics, 财务表支持手动同步。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._em_task: asyncio.Task | None = None
        self._running = False
        self._data_dir: Path | None = None
        self._capset: CapabilitySet | None = None
        self._lock = threading.Lock()
        self._last_sync: dict[str, str] = {}  # {table: iso_timestamp}
        # 手动同步(run_now)是否正在进行。前端据此显示"同步中"并防重复点击。
        self._is_syncing = False
        # 自选变动请求的补数时间戳(防抖): 非零且已到点 → 补数循环尽快执行一次
        self._supplement_requested_at: float = 0.0
        # 每日定点补数是否已跑过: (year, yday) 或 None
        self._last_daily_supplement: tuple[int, int] | None = None

    def start(self, data_dir: Path, capset: CapabilitySet, *, auto_schedule: bool = False) -> None:
        """初始化调度器，并按需启动周期同步后台任务。

        auto_schedule=False (默认): 仅初始化 (设置数据目录/能力 + 恢复 last_sync),
            供 /api/financials/sync/* 手动同步使用, 不启动自动调度。
        auto_schedule=True: 额外启动每周一次的 metrics 自动同步 (启动后 60s 首跑)。
        """
        # 先记录 data_dir/capset, 即使当前无 FINANCIAL 也保留引用:
        # 用户稍后在「设置」页升级到 Expert Key 时, update_capabilities() 会把新 capset
        # 推进来,trigger()/run_now() 才能用上 FINANCIAL。否则 _capset 永远是 None,
        # 即便 app.state.capabilities 已更新, 调度器仍报 "no FINANCIAL capability"。
        self._data_dir = data_dir
        self._capset = capset
        if not capset.has(Cap.FINANCIAL) and not _financial_is_custom():
            logger.info("FinancialScheduler skipped: no FINANCIAL capability")
            return
        # 从持久化恢复上次同步时间: 重启后前端仍能显示真实最后同步时间,而非"尚未同步"
        try:
            from app.services import preferences
            restored = dict(preferences.get_financial_sync_times())
            # 老用户迁移兜底: 若某表在 preferences 无记录但 parquet 已存在(升级前同步过),
            # 用 parquet 文件的修改时间作为同步时间并补写持久化。
            for table in FINANCIAL_TABLES:
                if table in restored:
                    continue
                parquet = data_dir / "financials" / table / "part.parquet"
                if parquet.exists():
                    mtime = datetime.fromtimestamp(parquet.stat().st_mtime, tz=timezone.utc).isoformat()
                    restored[table] = mtime
                    preferences.set_financial_sync_time(table, mtime)
                    logger.info("FinancialScheduler backfilled last_sync for %s from parquet mtime", table)
            self._last_sync = restored
            if self._last_sync:
                logger.info("FinancialScheduler restored last_sync: %s", list(self._last_sync.keys()))
        except Exception as e:  # noqa: BLE001
            logger.warning("restore financial_sync_times failed: %s", e)

        if not auto_schedule:
            # 仅初始化 (手动同步用), 不启动周期任务。
            logger.info("FinancialScheduler initialized (auto-schedule disabled; manual sync only)")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        self._em_task = asyncio.create_task(self._run_em_supplement_loop())
        logger.info("FinancialScheduler started (auto-schedule enabled)")

    def _record_sync(self, table: str) -> None:
        """记录一张表的同步完成时间: 更新内存 + 持久化到 preferences.json。

        持久化确保即使重启,前端 /status 仍返回真实的最后同步时间,
        不会错误地显示"尚未同步"。
        """
        ts = datetime.now(timezone.utc).isoformat()
        self._last_sync[table] = ts
        try:
            from app.services import preferences
            preferences.set_financial_sync_time(table, ts)
        except Exception as e:  # noqa: BLE001
            logger.warning("persist financial_sync_time(%s) failed: %s", e)

    def update_capabilities(self, capset: CapabilitySet) -> None:
        """刷新调度器持有的能力集。

        用户在「设置」页新增/清除 API Key 后, settings API 会重新探测能力并更新
        app.state.capabilities; 必须同步推给本调度器, 否则 trigger()/run_now() 仍读
        启动时的旧 capset, 即便 app.state 已含 FINANCIAL, 调度器仍报
        "no FINANCIAL capability" 而拒绝同步 (表现为前端「全部同步」按钮闪一下无动作)。
        """
        prev = self._capset
        self._capset = capset
        had = bool(prev) and prev.has(Cap.FINANCIAL)
        now = capset.has(Cap.FINANCIAL)
        if had != now:
            logger.info(
                "FinancialScheduler capabilities updated: FINANCIAL %s -> %s", had, now
            )

    def stop(self) -> None:
        self._running = False
        for t in (self._task, self._em_task):
            if t:
                t.cancel()
        self._task = None
        self._em_task = None
        logger.info("FinancialScheduler stopped")

    def _metrics_due(self) -> bool:
        """距上次 metrics 同步是否已满 7 天。

        防止"启动后 60s 首跑"在每次重启时都重拉全市场 (约 1.1 万个请求、
        20~40 分钟): _last_sync 从 preferences.json 恢复, 7 天内跑过就跳过。
        """
        last = self._last_sync.get("metrics")
        if not last:
            return True  # 从未同步过 → 跑
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - last_dt).total_seconds() >= 7 * 86_400
        except ValueError:
            return True

    async def _run_loop(self) -> None:
        """每周执行一次 metrics 同步; 每天盘后跑一次东财自选股补数。"""
        try:
            while self._running:
                # 首次启动等 60s, 之后每 7 天执行一次
                await asyncio.sleep(60)
                if not self._running:
                    break

                # 每周: 只同步 metrics (7 天内跑过则跳过, 重启不再重拉全市场)
                if not self._metrics_due():
                    logger.info("FinancialScheduler: metrics synced within 7d, skip")
                else:
                    try:
                        # 在线程池跑: sync_metrics 是同步阻塞且请求量大
                        # (fuyao 指标需逐股反查利润表), 直接 await 会卡死事件循环
                        rows = await asyncio.to_thread(
                            sync_metrics, self._data_dir, self._capset
                        )
                        self._record_sync("metrics")
                        logger.info("FinancialScheduler: metrics synced, %d rows", rows)
                    except Exception as e:
                        logger.warning("FinancialScheduler: metrics sync failed: %s", e)

                # 等待下一次 (7天)
                for _ in range(7 * 24 * 60):  # 每分钟检查一次 _running
                    if not self._running:
                        break
                    await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass

    def request_supplement(self, delay_s: float = 20.0) -> None:
        """请求尽快跑一次自选股双源补数 (自选股增删后由 watchlist API 调用)。

        防抖: 连续添加多只时取最晚到期时间, 只跑一次。
        补数循环每 30s 醒来检查, 实际执行 ≈ delay_s + 30s 内。
        """
        import time as _time

        self._supplement_requested_at = max(
            self._supplement_requested_at, _time.time() + delay_s
        )
        logger.info("watchlist_supplement requested (debounce %.0fs)", delay_s)

    def _run_supplement_once(self) -> None:
        """带并发保护地跑一次补数 (与手动/全量同步共用 _is_syncing, 防止并发写 parquet)。"""
        if not self._data_dir or not self._capset:
            return
        with self._lock:
            if self._is_syncing:
                logger.info("watchlist_supplement skipped: another sync running")
                return
            self._is_syncing = True
        try:
            sync_watchlist_supplement(self._data_dir, self._capset)
        finally:
            with self._lock:
                self._is_syncing = False

    async def _run_em_supplement_loop(self) -> None:
        """自选股双源补数循环: 每天 16:0x (A股盘后) 一次 + 自选变动即时触发。

        每 30s 醒一次检查: (a) 是否到每日定点, (b) 是否有防抖到期的即时请求。
        补数本体在线程池跑(同步阻塞 + 网络请求), 不卡事件循环;
        失败只记日志 — 定点路径次日重试, 即时路径由下次自选变动再触发。
        """
        import time as _time

        def _daily_due(now_ts: float) -> bool:
            """今天 16:07 及以后, 且今天还没跑过定点补数。"""
            lt = _time.localtime(now_ts)
            if lt.tm_hour != _EM_SUPPLEMENT_HOUR or lt.tm_min < 7:
                return False
            return self._last_daily_supplement != (lt.tm_year, lt.tm_yday)

        try:
            self._last_daily_supplement = None
            while self._running:
                now = _time.time()
                lt = _time.localtime(now)
                today = (lt.tm_year, lt.tm_yday)

                req_due = self._supplement_requested_at > 0 and now >= self._supplement_requested_at
                daily = _daily_due(now)
                if req_due or daily:
                    if req_due:
                        self._supplement_requested_at = 0.0
                    if daily:
                        self._last_daily_supplement = today
                    try:
                        await asyncio.to_thread(self._run_supplement_once)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("watchlist_supplement loop failed: %s", e)
                    # 跑完睡 60s 再进下一轮检查, 防止同秒重复触发
                    await asyncio.sleep(60)
                    continue

                # 下一次醒来时间: min(30s, 即时请求到期时间)
                nap = 30.0
                if self._supplement_requested_at > 0:
                    nap = min(nap, max(1.0, self._supplement_requested_at - now))
                # 分片等待, 保证 stop() 能及时退出
                for _ in range(int(nap // 30) + 1):
                    if not self._running:
                        return
                    await asyncio.sleep(min(30.0, nap))
        except asyncio.CancelledError:
            pass

    def _run_body(self, table: str | None, scope: str = "all") -> dict[str, int]:
        """同步逻辑本体(不加锁,假设调用方已持有 _is_syncing)。

        table=None 同步全部财务表;否则只同步指定表。
        scope="watchlist" 仅同步自选股 (增量, 不冲掉其他股票)。
        每张表完成立即更新 last_sync,让前端轮询 /status 能看到进度递增。
        """
        if table:
            fn = {
                "metrics": sync_metrics,
                "income": sync_income,
                "balance_sheet": sync_balance_sheet,
                "cash_flow": sync_cash_flow,
                "shares": sync_shares,
            }.get(table)
            if not fn:
                return {}
            rows = fn(self._data_dir, self._capset, scope=scope)
            self._record_sync(table)
            return {table: rows}
        # 全部同步
        symbols, scope = _resolve_symbols(self._data_dir, scope)
        if not symbols:
            logger.info("financial sync skipped: no symbols (scope=%s)", scope)
            return {}
        result: dict[str, int] = {}
        for t in FINANCIAL_TABLES:
            result[t] = _sync_history_table_for_symbols(
                t, symbols, self._data_dir, self._capset
            )
            self._record_sync(t)
        _refresh_financials_views(self._data_dir)
        return result

    def run_now(self, table: str | None = None, scope: str = "all") -> dict[str, int]:
        """同步执行一次同步(阻塞调用线程)。

        ⚠ 全量同步需数分钟,务必在后台线程调用,不要直接在 HTTP 请求线程里阻塞,
        否则请求会长时间 pending 直至被浏览器/代理超时掐断(表现为"点击无反应")。
        HTTP 接口应调用 trigger() 立即返回,再让前端轮询 /status.syncing 看进度。

        用 _is_syncing 标志防并发:若已有同步在进行,本次直接跳过,
        避免重复请求拖慢服务端 / 触发上游限流。
        """
        if not self._capset or (not self._capset.has(Cap.FINANCIAL) and not _financial_is_custom()):
            return {}
        with self._lock:
            if self._is_syncing:
                logger.info("financial sync skipped: already running")
                return {"_skipped": 1}
            self._is_syncing = True
        try:
            return self._run_body(table, scope=scope)
        finally:
            with self._lock:
                self._is_syncing = False

    def trigger(self, table: str | None = None, scope: str = "all") -> dict[str, int]:
        """触发一次同步(非阻塞,立即返回)。

        在后台线程执行同步体,HTTP 请求无需等待。
        返回 {"started": True/False}:
          - False = 能力不足或已有同步在进行(被防并发跳过)
          - True  = 已在后台开始,前端应轮询 /status.syncing 观察进度

        ⚠ _is_syncing 在此处置 True(持锁),确保 trigger 返回时前端轮询
        /status 已能看到 syncing=True,无竞态窗口;同时防止快速重复点击
        启动多个后台线程。后台线程复用 _run_body 执行真正的同步逻辑。
        """
        if not self._capset or (not self._capset.has(Cap.FINANCIAL) and not _financial_is_custom()):
            return {"started": False, "reason": "no FINANCIAL capability"}
        with self._lock:
            if self._is_syncing:
                logger.info("financial sync trigger skipped: already running")
                return {"started": False, "reason": "already running"}
            # 持锁置位:保证 trigger 返回前 syncing 已为 True
            self._is_syncing = True

        def _bg() -> None:
            try:
                self._run_body(table, scope=scope)
            except Exception as e:  # noqa: BLE001
                logger.exception("background financial sync failed: %s", e)
            finally:
                with self._lock:
                    self._is_syncing = False

        t = threading.Thread(target=_bg, name="financial-sync", daemon=True)
        t.start()
        logger.info(
            "financial sync triggered in background: table=%s scope=%s",
            table or "all", scope,
        )
        return {"started": True}

    @property
    def is_syncing(self) -> bool:
        """手动同步是否正在进行(供 /status 返回,前端据此显示"同步中")。"""
        with self._lock:
            return self._is_syncing

    @property
    def last_sync(self) -> dict[str, str]:
        return dict(self._last_sync)


# 全局单例
financial_scheduler = FinancialScheduler()
