import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/restart-admission'
const PAGE_SIZE = 10
const columns = [
  '准入单号',
  '重启请求号',
  '实例编号',
  '运行节点',
  '检查点编号',
  '检查点版本',
  '需要slot数',
  '节点空闲slot',
  '缺失slot',
  '准入结论',
  '快照版本',
  '创建时间',
]
const conclusions = ['准入通过', '准入驳回', '准入回退']

type Row = Record<string, string | number | boolean | null>
type Filters = { keyword: string; conclusion: string; instance_no: string; node: string }

const emptyFilters: Filters = { keyword: '', conclusion: '', instance_no: '', node: '' }

export default function RestartAdmissionView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [filters, setFilters] = useState<Filters>(emptyFilters)
  const [appliedFilters, setAppliedFilters] = useState<Filters>(emptyFilters)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [detail, setDetail] = useState<Row | null>(null)
  const [stats, setStats] = useState({ total: 0, passed: 0, denied: 0, rolledBack: 0 })
  const [submitForm, setSubmitForm] = useState({ instance_id: '', request_no: '', required_slots: '' })

  const reload = useCallback(async () => {
    setErrorMessage('')
    const params = new URLSearchParams()
    Object.entries(appliedFilters).forEach(([key, value]) => {
      if (value) params.set(key, value)
    })
    params.set('page', String(page))
    params.set('size', String(PAGE_SIZE))
    try {
      const response = await request(`${ENDPOINT}?${params.toString()}`)
      if (!response.ok) throw new Error('重启准入清单读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '重启准入清单读取失败')
    }
  }, [appliedFilters, page])

  const reloadStats = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}?size=200`)
      if (!response.ok) return
      const payload = await response.json()
      const items = (payload.items ?? []) as Row[]
      setStats({
        total: items.length,
        passed: items.filter((item) => item['准入结论'] === '准入通过').length,
        denied: items.filter((item) => item['准入结论'] === '准入驳回').length,
        rolledBack: items.filter((item) => item['准入结论'] === '准入回退').length,
      })
    } catch {
      // 统计卡片加载失败不阻塞主清单
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void reloadStats()
  }, [reloadStats, rows])

  const pageCount = useMemo(() => Math.max(Math.ceil(total / PAGE_SIZE), 1), [total])

  function applyFilters() {
    setPage(1)
    setAppliedFilters({ ...filters })
  }

  async function openDetail(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      if (!response.ok) throw new Error('准入单详情读取失败')
      setDetail(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '准入单详情读取失败')
    }
  }

  async function submitAdmission() {
    setErrorMessage('')
    setNotice('')
    const instanceId = Number(submitForm.instance_id)
    const requestNo = submitForm.request_no.trim()
    if (!Number.isInteger(instanceId) || instanceId <= 0) {
      setErrorMessage('作业实例 id 必须为正整数')
      return
    }
    if (!requestNo) {
      setErrorMessage('重启请求号必填，作为并发提交的幂等键')
      return
    }
    const values: Record<string, string | number> = { instance_id: instanceId, request_no: requestNo }
    if (submitForm.required_slots.trim()) {
      const slots = Number(submitForm.required_slots)
      if (!Number.isInteger(slots) || slots <= 0) {
        setErrorMessage('需要 slot 数必须为正整数，或留空取实例配置')
        return
      }
      values.required_slots = slots
    }
    try {
      const response = await request(`${ENDPOINT}/submit`, {
        method: 'POST',
        body: JSON.stringify({ values }),
      })
      const payload = await response.json()
      // 驳回也会落库生成准入单，需要明确提示而不是当作静默失败。
      setNotice(payload.message ?? '准入已提交')
      setSubmitForm({ instance_id: '', request_no: '', required_slots: '' })
      await reload()
      await reloadStats()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '重启准入提交失败')
    }
  }

  async function rollback(row: Row) {
    setErrorMessage('')
    setNotice('')
    const failReason = window.prompt(`准入单 ${row['准入单号']} 恢复失败回退，请填写失败原因：`)
    if (failReason === null) return
    try {
      const response = await request(`${ENDPOINT}/${row.id}/rollback`, {
        method: 'POST',
        body: JSON.stringify({ values: { fail_reason: failReason || '恢复过程失败' } }),
      })
      const payload = await response.json()
      if (!payload.ok) throw new Error(payload.message ?? '回退未生效')
      setNotice(payload.message)
      setDetail(null)
      await reload()
      await reloadStats()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '准入回退失败')
    }
  }

  return (
    <section className="page" data-module="restart-admission">
      <header className="page-head">
        <div>
          <h2>重启准入数据清单</h2>
          <p className="page-desc">
            作业实例重启前联合校验检查点可恢复状态、状态后端生效配置与节点 slot 容量；
            清单固化检查点版本、缺失 slot 与准入结论，列表与详情读取同一份快照。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出准入清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">准入单总数</span>
          <strong className="stat-value">{stats.total}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">准入通过</span>
          <strong className="stat-value">{stats.passed}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">准入驳回（容量/检查点不满足）</span>
          <strong className="stat-value">{stats.denied}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">恢复失败已回退</span>
          <strong className="stat-value">{stats.rolledBack}</strong>
        </article>
      </div>

      <form
        className="filter-bar"
        onSubmit={(event) => {
          event.preventDefault()
          applyFilters()
        }}
      >
        <label className="filter-item">
          <span>关键词</span>
          <input
            value={filters.keyword}
            placeholder="准入单号/请求号/实例/检查点版本"
            onChange={(event) => setFilters((prev) => ({ ...prev, keyword: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>准入结论</span>
          <select
            value={filters.conclusion}
            onChange={(event) => setFilters((prev) => ({ ...prev, conclusion: event.target.value }))}
          >
            <option value="">全部结论</option>
            {conclusions.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label className="filter-item">
          <span>实例编号</span>
          <input
            value={filters.instance_no}
            placeholder="如 JOBO-0002"
            onChange={(event) => setFilters((prev) => ({ ...prev, instance_no: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>运行节点</span>
          <input
            value={filters.node}
            placeholder="如 node-b"
            onChange={(event) => setFilters((prev) => ({ ...prev, node: event.target.value }))}
          />
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => { setFilters(emptyFilters); setAppliedFilters(emptyFilters); setPage(1) }}>
          重置条件
        </button>
      </form>

      <div className="submit-panel">
        <h3>提交重启准入</h3>
        <p className="page-desc">同一重启请求号并发提交只生效一次；检查点与状态后端缺省取实例当前绑定配置，全部校验通过后才占用 slot。</p>
        <div className="filter-bar">
          <label className="filter-item">
            <span>作业实例 id</span>
            <input
              value={submitForm.instance_id}
              placeholder="如 2（JOBO-0002）"
              onChange={(event) => setSubmitForm((prev) => ({ ...prev, instance_id: event.target.value }))}
            />
          </label>
          <label className="filter-item">
            <span>重启请求号（幂等键）</span>
            <input
              value={submitForm.request_no}
              placeholder="如 restart-16"
              onChange={(event) => setSubmitForm((prev) => ({ ...prev, request_no: event.target.value }))}
            />
          </label>
          <label className="filter-item">
            <span>需要 slot 数（选填）</span>
            <input
              value={submitForm.required_slots}
              placeholder="留空取实例配置"
              onChange={(event) => setSubmitForm((prev) => ({ ...prev, required_slots: event.target.value }))}
            />
          </label>
          <button className="btn primary" type="button" onClick={() => void submitAdmission()}>联合准入校验</button>
        </div>
      </div>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => (
                <td key={column}>
                  {column === '准入结论' ? (
                    <span className={`badge badge-${String(row[column])}`}>{String(row[column] ?? '—')}</span>
                  ) : column === '缺失slot' && Number(row[column]) > 0 ? (
                    <span className="slot-missing">{String(row[column])}</span>
                  ) : (
                    String(row[column] ?? '—')
                  )}
                </td>
              ))}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openDetail(row)}>查看详情</button>
                {row['准入结论'] === '准入通过' && (
                  <button className="link danger" type="button" onClick={() => void rollback(row)}>恢复失败回退</button>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无符合条件的重启准入记录</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条准入记录，第 {page} / {pageCount} 页</span>
        <span className="pager">
          <button className="btn" type="button" disabled={page <= 1} onClick={() => setPage((prev) => Math.max(prev - 1, 1))}>上一页</button>
          <button className="btn" type="button" disabled={page >= pageCount} onClick={() => setPage((prev) => Math.min(prev + 1, pageCount))}>下一页</button>
        </span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {detail && (
        <div className="modal-mask" onClick={() => setDetail(null)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <header className="modal-head">
              <h3>准入单 {String(detail['准入单号'])}</h3>
              <button className="btn ghost" type="button" onClick={() => setDetail(null)}>关闭</button>
            </header>
            <dl className="detail-grid">
              {[
                ['重启请求号', '重启请求号'],
                ['实例编号', '实例编号'],
                ['作业名称', '作业名称'],
                ['运行节点', '运行节点'],
                ['重启次数', '重启次数'],
                ['恢复条件', '恢复条件'],
                ['检查点编号', '检查点编号'],
                ['检查点版本', '检查点版本'],
                ['状态后端编号', '状态后端编号'],
                ['状态后端版本', '状态后端版本'],
                ['需要slot数', '需要slot数'],
                ['节点slot总数', '节点slot总数'],
                ['节点空闲slot', '节点空闲slot'],
                ['缺失slot', '缺失slot'],
                ['准入结论', '准入结论'],
                ['准入说明', '准入说明'],
                ['快照版本', '快照版本'],
                ['处理日志编号', '处理日志编号'],
                ['创建时间', '创建时间'],
                ['生效时间', '生效时间'],
                ['回退原因', '回退原因'],
              ].map(([label, key]) => (
                <div className="detail-item" key={key}>
                  <dt>{label}</dt>
                  <dd>{String(detail[key] ?? '—')}</dd>
                </div>
              ))}
            </dl>
          </div>
        </div>
      )}
    </section>
  )
}
