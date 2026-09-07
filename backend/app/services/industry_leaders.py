"""行业龙头候选 — 基本面初筛 Top N 的后台数据管道。

与行业分析页「基本面龙头」模式配套的分层设计:
1. 初筛: 全市场单期数据(市值/最新ROE/毛利率/营收)行业内归一化打分, 每个二级行业取 Top N;
2. 补数: 候选清单存 preferences(industry_leader_candidates), 由独立的
   financial_sync.sync_leader_candidates 后台拉深历史(东财全量 + fuyao 深历史 +
   指标逐期), 与自选股通道(sync_watchlist_supplement)完全解耦, 不往 watchlist 写任何东西;
3. 刷新: 年度(5/6 后年报入库)自动重算 + 手动按钮任意时点重算, 更新清单;
   落榜股票 parquet 数据保留, 只是停止增量。

打分公式与前端 IndustryAnalysis.tsx 的 calcFundamentals 保持一致(改权重需两边同步)。
行业分组取内置预设 ext_hy_ths 的二级行业(与行业分析页默认层级一致)。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

# 与前端 FUND_WEIGHTS 一致
FUND_WEIGHTS = {"mcap": 0.35, "roe": 0.25, "gm": 0.20, "revenue": 0.20}
DEFAULT_TOP_N = 5

_CANDIDATES_PREFS_KEY = "industry_leader_candidates"
_DONE_PREFS_KEY = "industry_leader_candidates_done"
_REFRESH_PREFS_KEY = "industry_leader_candidates_refresh_year"
# 年报 4/30 披露截止, 留几天余量等全市场同步把新年报落进 parquet
_REFRESH_MONTH_DAY = (5, 6)
# 全市场 income 出现上一年年报期(前一年-12-31)的最少行数, 判定年报数据已入库
_ANNUAL_READY_MIN_ROWS = 3000


# ===== 数据组装 (与 /api/screener/fundamental-snapshot 端点共用) =====

def load_fundamental_snapshot(data_dir: Path, repo=None) -> tuple[str | None, pl.DataFrame]:
    """每股最新市值 + 最新一期财务指标 + 上市日期。返回 (as_of, df)。

    数据源: 日K enriched 最新分区(市值) / financials/metrics 每股最新期 /
    financials/income 每股最新期(revenue 列, total_revenue 历史行多为空) / instruments。
    """
    from app.services.screener import ScreenerService

    if repo is None:
        from app.tickflow.repository import DataStore, KlineRepository
        repo = KlineRepository(DataStore(Path(data_dir)))
    svc = ScreenerService(repo)
    data_dir = Path(data_dir)
    as_of = svc.latest_date()
    if not as_of:
        return None, pl.DataFrame()

    df = svc._load_enriched_for_date(as_of)
    if df.is_empty():
        return str(as_of), pl.DataFrame()
    if "close" in df.columns and "total_shares" in df.columns and "market_cap" not in df.columns:
        df = df.with_columns((pl.col("close") * pl.col("total_shares")).alias("market_cap"))
    if "close" in df.columns and "float_shares" in df.columns and "float_market_cap" not in df.columns:
        df = df.with_columns((pl.col("close") * pl.col("float_shares")).alias("float_market_cap"))

    base = df.select([c for c in ["symbol", "name", "market_cap", "float_market_cap"] if c in df.columns])

    def _latest_per_symbol(table: str, columns: list[str]):
        """读取财务表并取每股最新一期; 表不存在/读失败返回 None。"""
        path = data_dir / "financials" / table / "part.parquet"
        if not path.exists():
            return None
        try:
            t = pl.read_parquet(path, columns=["symbol", "period_end", *columns])
        except Exception:
            logger.warning("fundamental snapshot: 读取 %s 失败", table, exc_info=True)
            return None
        if t.is_empty():
            return None
        return t.sort("period_end").group_by("symbol").last()

    metrics = _latest_per_symbol("metrics", ["roe", "gross_margin", "net_margin", "revenue_yoy"])
    if metrics is not None:
        base = base.join(metrics, on="symbol", how="left")

    income = _latest_per_symbol("income", ["revenue"])
    if income is not None:
        # period_end 改名避免与 metrics 的报告期冲突
        income = income.select(
            "symbol",
            pl.col("revenue").alias("revenue"),
            pl.col("period_end").alias("income_period"),
        )
        base = base.join(income, on="symbol", how="left")

    inst_path = data_dir / "instruments" / "instruments.parquet"
    if inst_path.exists():
        try:
            inst = pl.read_parquet(inst_path, columns=["symbol", "listing_date"])
            base = base.join(inst, on="symbol", how="left")
        except Exception:
            logger.warning("fundamental snapshot: 读取 instruments 失败", exc_info=True)

    # NaN → null 统一口径 (东财/扶摇偶发 NaN, null 语义在打分/前端一致)
    float_cols = [c for c in base.columns if base.schema[c] in (pl.Float64, pl.Float32)]
    if float_cols:
        base = base.with_columns([
            pl.when(pl.col(c).is_nan()).then(None).otherwise(pl.col(c)).alias(c)
            for c in float_cols
        ])
    return str(as_of), base


# ===== 行业成员 =====

def _industry_members(data_dir: Path) -> pl.DataFrame | None:
    """内置行业预设 ext_hy_ths → [symbol, industry(二级行业)]。未获取返回 None。"""
    path = Path(data_dir) / "ext_data" / "ext_hy_ths" / "part.parquet"
    if not path.exists():
        return None
    df = pl.read_parquet(path, columns=["symbol", "所属同花顺行业"])
    parts = pl.col("所属同花顺行业").str.split("-").list.len()
    return df.with_columns(
        pl.when(parts >= 2)
        .then(pl.col("所属同花顺行业").str.split("-").list[1])
        .otherwise(pl.col("所属同花顺行业"))
        .str.strip_chars()
        .alias("industry")
    ).select("symbol", "industry")


# ===== 打分 (复刻前端 calcFundamentals) =====

def _minmax(values: list[float]):
    if not values:
        return lambda v: 0.5
    lo, hi = min(values), max(values)
    if hi == lo:
        return lambda v: 0.5
    return lambda v: max(0.0, min(1.0, (v - lo) / (hi - lo)))


def _score_industry(rows: list[dict], hide_gm: bool) -> list[tuple[dict, float]]:
    """对一个行业的全部股票打分, 返回有资格股票的 (row, score) 降序列表。"""
    one_year_ago = datetime.now().timestamp() - 365 * 24 * 3600

    def _listed_ok(listing_date) -> bool:
        if not listing_date:
            return True
        try:
            lt = datetime.fromisoformat(str(listing_date))
        except ValueError:
            return True
        return lt.timestamp() <= one_year_ago

    enriched = []
    for r in rows:
        name = str(r.get("name") or "")
        eligible = (
            "ST" not in name
            and _listed_ok(r.get("listing_date"))
            and (r.get("roe") is not None or r.get("revenue") is not None)
        )
        enriched.append({**r, "eligible": eligible})

    # 行业内 min-max 归一化(金额类 log 压缩长尾), 分母含全部有值股票
    norm_mcap = _minmax([math.log1p(r["market_cap"]) for r in enriched if r.get("market_cap") is not None])
    norm_roe = _minmax([r["roe"] for r in enriched if r.get("roe") is not None])
    norm_gm = _minmax([r["gross_margin"] for r in enriched if r.get("gross_margin") is not None])
    norm_rev = _minmax([math.log1p(r["revenue"]) for r in enriched if r.get("revenue") is not None])

    weights = dict(FUND_WEIGHTS)
    if hide_gm:
        weights["gm"] = 0.0
    weight_sum = sum(weights.values())

    scored = []
    for r in enriched:
        if not r["eligible"]:
            continue
        part_mcap = norm_mcap(math.log1p(r["market_cap"])) if r.get("market_cap") is not None else 0.5
        part_roe = norm_roe(r["roe"]) if r.get("roe") is not None else 0.5
        part_gm = 0.5 if hide_gm or r.get("gross_margin") is None else norm_gm(r["gross_margin"])
        part_rev = norm_rev(math.log1p(r["revenue"])) if r.get("revenue") is not None else 0.5
        score = (
            part_mcap * weights["mcap"] + part_roe * weights["roe"]
            + part_gm * weights["gm"] + part_rev * weights["revenue"]
        ) / weight_sum * 100
        scored.append((r, score))
    scored.sort(key=lambda t: -t[1])
    return scored


def compute_candidates(data_dir: Path, top_n: int = DEFAULT_TOP_N) -> dict:
    """每个二级行业取基本面 Top N 候选。返回 {by_industry, total, as_of}。"""
    as_of, fund = load_fundamental_snapshot(Path(data_dir))
    if fund.is_empty():
        return {"status": "error", "message": "无行情数据(kline_daily_enriched 为空)"}
    members = _industry_members(Path(data_dir))
    if members is None or members.is_empty():
        return {"status": "error", "message": "行业数据未获取(缺少 ext_hy_ths), 请先在行业分析页获取行业数据"}

    df = members.join(fund, on="symbol", how="left")
    by_industry: dict[str, list[str]] = {}
    for (industry,), grp in df.group_by("industry"):
        rows = grp.to_dicts()
        hide_gm = "银行" in str(industry)
        top = _score_industry(rows, hide_gm)[:top_n]
        by_industry[str(industry)] = [r["symbol"] for r, _ in top]
    total = sum(len(v) for v in by_industry.values())
    return {"status": "ok", "as_of": as_of, "by_industry": by_industry, "total": total}


# ===== 候选清单管理 (preferences 持久化, 不碰 watchlist) =====

def list_candidates() -> list[str]:
    """当前候选清单 (symbol 列表)。"""
    from app.services import preferences
    return list(preferences.load().get(_CANDIDATES_PREFS_KEY) or [])


def _done_symbols() -> set[str]:
    """已完成深历史补数的候选 (429 等失败的下轮重试)。"""
    from app.services import preferences
    return set(preferences.load().get(_DONE_PREFS_KEY) or [])


def _mark_done(symbols: list[str]) -> None:
    from app.services import preferences
    preferences.save({_DONE_PREFS_KEY: sorted(_done_symbols() | set(symbols))})


def update_candidates(data_dir: Path, top_n: int = DEFAULT_TOP_N) -> dict:
    """初筛 Top N → 更新 preferences 里的候选清单 (年度自动刷新与手动按钮共用)。

    新入榜者记入清单待补数; 落榜者从清单移除(已补的 parquet 数据保留, done
    标记也保留 — 重新入榜零成本复用)。幂等, 不写 watchlist 任何内容。
    """
    from app.services import preferences

    result = compute_candidates(data_dir, top_n)
    if result.get("status") != "ok":
        return result

    new_list = sorted({s for syms in result["by_industry"].values() for s in syms})
    old_list = list_candidates()

    preferences.save({
        _CANDIDATES_PREFS_KEY: new_list,
        _REFRESH_PREFS_KEY: datetime.now().year,
    })

    added = sorted(set(new_list) - set(old_list))
    removed = sorted(set(old_list) - set(new_list))
    logger.info(
        "industry_leaders update_candidates: %d 行业 %d 候选 (新增 %d, 移出 %d)",
        len(result["by_industry"]), len(new_list), len(added), len(removed),
    )
    return {
        "status": "ok",
        "as_of": result["as_of"],
        "industries": len(result["by_industry"]),
        "candidates": len(new_list),
        "added": added,
        "removed": removed,
        "by_industry": result["by_industry"],
    }


# ===== 年度自动刷新 =====

def refresh_if_due(data_dir: Path) -> dict | None:
    """每年 5/6 后且今年未刷新过 → 检查年报数据已入库 → update_candidates。

    挂在每日 16:07 补数调度最前: 新候选当天即进入补数通道。
    年报数据未就绪时跳过, 次日再查 (全市场同步刷新后自动完成)。
    """
    from app.services import preferences
    from app.services.financial_sync import get_financial_df

    now = datetime.now()
    if (now.month, now.day) < _REFRESH_MONTH_DAY:
        return None
    if preferences.load().get(_REFRESH_PREFS_KEY) == now.year:
        return None

    prev_annual = f"{now.year - 1}-12-31"
    inc = get_financial_df(Path(data_dir), "income")
    if inc.is_empty() or "period_end" not in inc.columns:
        return {"status": "wait_data", "reason": "income 表为空"}
    annual_rows = inc.filter(pl.col("period_end") == prev_annual).height
    if annual_rows < _ANNUAL_READY_MIN_ROWS:
        logger.info(
            "industry_leaders refresh: 等待年报入库 (%s 行 %d < %d)",
            prev_annual, annual_rows, _ANNUAL_READY_MIN_ROWS,
        )
        return {"status": "wait_data", "annual_rows": annual_rows}
    logger.info("industry_leaders refresh: 年度刷新候选 (年报 %s 已入库 %d 行)", prev_annual, annual_rows)
    return update_candidates(Path(data_dir))
