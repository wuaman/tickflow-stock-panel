"""东方财富 datacenter 财报 HTTP 适配插件 (shim).

把 shim 从运行时 data/ 目录迁入项目正式模块, 打进镜像, 不再依赖 data/。
可作为 sidecar 服务运行 (见 docker-compose.yml 的 financial-shim service),
也可被主进程 import 复用。

为什么需要这个 shim:
  项目的「自定义财务 HTTP 源」是单个 `financial` dataset, 请求时把 table
  (metrics/income/balance_sheet/cash_flow) 作为 query 参数传给上游。
  但东方财富 datacenter 按 `reportName` 区分四张表, 且 reportName 必须写死在 URL 里,
  纯 YAML 一个 URL 无法服务多张表。本模块接收 table 参数, 内部路由到对应 reportName,
  返回 {data: [rows]} 供 YAML field_map 映射。

运行 (容器内已含 fastapi/httpx):
  uv run uvicorn app.plugins.eastmoney_shim:app --host 0.0.0.0 --port 3021

合规提示: 东方财富 datacenter 属未公开文档化接口, 无 SLA, 高频调用有封 IP 风险。
         已加符号间小睡 (EM_SLEEP, 默认 0.3s) 与浏览器 UA/Referer 降低风险, 生产请再加缓存。
"""
from __future__ import annotations

import logging
import os
import time

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [shim] %(message)s")
log = logging.getLogger("em_shim")

app = FastAPI(title="eastmoney-financial-shim")

EM_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 项目 table 名 -> 东方财富 reportName
REPORT = {
    "metrics": "RPT_LICO_FN_CPD",
    "income": "RPT_DMSK_FN_INCOME",
    "balance_sheet": "RPT_DMSK_FN_BALANCE",
    "cash_flow": "RPT_DMSK_FN_CASHFLOW",
}

SLEEP = float(os.getenv("EM_SLEEP", "0.3"))


def _coerce(row: dict) -> dict:
    """统一数值类型: int -> float, 避免 polars 按首行推断 i64 后与 float 行冲突。
    字符串/None 原样返回。"""
    out = {}
    for k, v in row.items():
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, int):
            out[k] = float(v)
        else:
            out[k] = v
    return out


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/financial")
def financial(
    table: str = Query(...),
    symbols: str = Query(""),
    start_time: str | None = None,   # noqa: ARG001
    end_time: str | None = None,     # noqa: ARG001
) -> dict:
    rn = REPORT.get(table)
    if not rn:
        return JSONResponse(
            {"data": [], "error": f"unknown table: {table}"}, status_code=400
        )
    syms = [s.strip() for s in (symbols or "").split(",") if s.strip()]
    out: list[dict] = []
    headers = {"User-Agent": UA, "Referer": "https://emweb.securities.eastmoney.com/"}
    with httpx.Client(timeout=20.0, headers=headers) as cli:
        for i, sym in enumerate(syms):
            try:
                r = cli.get(
                    EM_URL,
                    params={
                        "reportName": rn,
                        "filter": f'(SECUCODE="{sym}")',
                        "columns": "ALL",
                        "pageSize": 200,
                        "pageNumber": 1,
                    },
                )
                payload = r.json()
            except Exception as e:  # noqa: BLE001
                log.warning("fetch %s/%s failed: %s", table, sym, e)
                continue
            if not payload.get("success"):
                log.warning("em %s/%s not success: %s", table, sym, payload.get("message"))
                continue
            rows = ((payload.get("result") or {}).get("data")) or []
            out.extend(_coerce(r) for r in rows)
            log.info("em %s %s -> %d rows", table, sym, len(rows))
            if i < len(syms) - 1 and SLEEP > 0:
                time.sleep(SLEEP)
    return {"data": out}
