import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/jobops'
const columns = [
  "实例编号",
  "作业名称",
  "运行节点",
  "启动时间",
  "运行时长",
  "异常次数",
  "重启次数",
  "需要slot数",
  "实例状态"
]
const actions = [
  "启动作业",
  "暂停作业",
  "停止作业",
  "重启作业"
]
const statuses = [
  "待启动",
  "运行中",
  "已暂停",
  "已停止"
]
const stats = [
  {
    "label": "作业实例总数",
    "value": 0
  },
  {
    "label": "运行中作业",
    "value": 0
  },
  {
    "label": "已暂停作业",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

export default function JobopsView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('作业实例列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '流作业运维列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        // 重启作业可能被准入门禁拦截，需要把后端的拦截原因展示给值班人员。
        throw new Error(payload.message ?? '流作业运维动作未生效，请稍后重试')
      }
      setNotice(payload.message ?? '操作已生效')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '流作业运维操作失败')
    }
  }

  async function checkAdmission(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/admission`)
      if (!response.ok) throw new Error('重启准入核对失败')
      const payload = await response.json()
      setNotice(
        `实例 ${payload['实例编号']}（节点 ${payload['运行节点']}，重启次数 ${payload['重启次数']}）` +
        `当前准入结论：${payload['准入结论']}` +
        (payload['检查点版本'] ? `，检查点版本 ${payload['检查点版本']}` : '') +
        (payload['缺失slot'] ? `，缺失 slot ${payload['缺失slot']} 个` : '')
      )
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '重启准入核对失败')
    }
  }

  return (
    <section className="page" data-module="jobops">
      <header className="page-head">
        <div>
          <h2>流作业运维管理</h2>
          <p className="page-desc">维护流作业的运行节点、启停管理与异常追踪；重启作业必须先通过重启准入数据清单的检查点与 slot 联合校验。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('作业实例登记入口尚未接入审批流')}>
            登记作业实例
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出流作业运维清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className={`link${action === '重启作业' ? ' danger' : ''}`} type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void checkAdmission(row)}>
                  准入核对
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无流作业运维数据，可先登记作业实例</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条流作业运维记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
