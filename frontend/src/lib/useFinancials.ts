import { useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'

export const FINANCIAL_QK = {
  status: ['financials', 'status'],
  metrics: (symbol?: string) => ['financials', 'metrics', symbol],
  income: (symbol?: string) => ['financials', 'income', symbol],
  balanceSheet: (symbol?: string) => ['financials', 'balance-sheet', symbol],
  cashFlow: (symbol?: string) => ['financials', 'cash-flow', symbol],
  shares: (symbol?: string) => ['financials', 'shares', symbol],
}

export function useFinancialStatus() {
  const qc = useQueryClient()
  // 上一次轮询到的 syncing 状态: 检测 true→false 跳变 (后台同步刚完成)。
  // 注意 trigger 的 onSuccess 在同步"开始"时触发, 那时数据还没写入;
  // 各表查询 staleTime=5min, 必须等同步真正结束后再失效, 否则刚同步的
  // 股票在缓存里还是"暂无数据"。
  const prevSyncing = useRef<boolean | null>(null)
  const query = useQuery({
    queryKey: FINANCIAL_QK.status,
    queryFn: () => api.financialStatus(),
    staleTime: 60_000,
    // 同步进行中时每 3s 轮询,及时反映表数变化与同步完成;空闲时不轮询。
    refetchInterval: (query) => (query.state.data?.syncing ? 3_000 : false),
  })
  const syncing = query.data?.syncing ?? false
  useEffect(() => {
    if (prevSyncing.current === true && !syncing) {
      qc.invalidateQueries({ queryKey: ['financials'] })
    }
    prevSyncing.current = syncing
  }, [syncing, qc])
  return query
}

export function financialMetricsQueryOptions(symbol?: string) {
  return {
    queryKey: FINANCIAL_QK.metrics(symbol),
    queryFn: () => api.financialMetrics(symbol),
    staleTime: 300_000,
  }
}

export function useFinancialMetrics(symbol?: string) {
  return useQuery({ ...financialMetricsQueryOptions(symbol), enabled: !!symbol })
}

export function useFinancialIncome(symbol?: string) {
  return useQuery({
    queryKey: FINANCIAL_QK.income(symbol),
    queryFn: () => api.financialIncome(symbol),
    enabled: !!symbol,
    staleTime: 300_000,
  })
}

export function useFinancialBalanceSheet(symbol?: string) {
  return useQuery({
    queryKey: FINANCIAL_QK.balanceSheet(symbol),
    queryFn: () => api.financialBalanceSheet(symbol),
    enabled: !!symbol,
    staleTime: 300_000,
  })
}

export function useFinancialCashFlow(symbol?: string) {
  return useQuery({
    queryKey: FINANCIAL_QK.cashFlow(symbol),
    queryFn: () => api.financialCashFlow(symbol),
    enabled: !!symbol,
    staleTime: 300_000,
  })
}

export function useFinancialShares(symbol?: string) {
  return useQuery({
    queryKey: FINANCIAL_QK.shares(symbol),
    queryFn: () => api.financialShares(symbol),
    enabled: !!symbol,
    staleTime: 300_000,
  })
}

export function useFinancialSync() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ table, scope }: { table: string; scope: 'all' | 'watchlist' }) =>
      api.financialSync(table, scope),
    // 点击瞬间立即刷新 status: 让后端 is_syncing=True 马上反映到 UI,
    // 避免 mutation 阻塞(全量同步需数分钟)期间界面无变化。
    onMutate: () => {
      qc.invalidateQueries({ queryKey: FINANCIAL_QK.status })
    },
    onSuccess: () => {
      // 仅刷 status 让 syncing 立刻反映; 各表缓存的失效由 useFinancialStatus
      // 检测 syncing true→false 跳变时统一处理 (trigger 返回时数据还没写完)。
      qc.invalidateQueries({ queryKey: FINANCIAL_QK.status })
    },
  })
}
