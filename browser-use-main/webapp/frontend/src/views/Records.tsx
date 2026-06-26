import { useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { api } from '../api'
import type { ImageRecord } from '../types'

const PAGE_SIZE = 50

const COLUMNS: { key: string; label: string; sortable?: boolean }[] = [
  { key: 'sequence', label: '#', sortable: true },
  { key: 'status', label: '状态', sortable: true },
  { key: 'title', label: '标题', sortable: true },
  { key: 'source_site', label: '来源站', sortable: true },
  { key: 'source_item_id', label: 'item id' },
  { key: 'file_name', label: '文件名' },
  { key: 'file_size', label: '大小', sortable: true },
  { key: 'downloaded_at', label: '下载时间', sortable: true },
]

function statusBadge(s: string) {
  const cls = s === 'downloaded' ? 'badge-downloaded' : s === 'failed' ? 'badge-failed' : 'badge-skipped'
  return <span className={`badge ${cls}`}>{s}</span>
}

function toCSV(rows: ImageRecord[]): string {
  const head = ['sequence', 'status', 'title', 'source_site', 'source_item_id', 'file_name', 'file_size', 'page_url', 'image_url', 'downloaded_at']
  const esc = (v: unknown) => {
    const s = v == null ? '' : String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const lines = [head.join(',')]
  for (const r of rows) lines.push(head.map((h) => esc((r as unknown as Record<string, unknown>)[h])).join(','))
  return lines.join('\n')
}

export default function Records() {
  const [page, setPage] = useState(1)
  const [q, setQ] = useState('')
  const [qInput, setQInput] = useState('')
  const [status, setStatus] = useState('')
  const [sort, setSort] = useState('sequence')
  const [order, setOrder] = useState<'asc' | 'desc'>('asc')

  const { data, isLoading } = useQuery({
    queryKey: ['records', page, q, status, sort, order],
    queryFn: () => api.images({ page, page_size: PAGE_SIZE, q, status, sort, order }),
    placeholderData: keepPreviousData,
  })

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1

  function toggleSort(key: string) {
    if (sort === key) setOrder((o) => (o === 'asc' ? 'desc' : 'asc'))
    else {
      setSort(key)
      setOrder('asc')
    }
    setPage(1)
  }

  function exportCSV() {
    if (!data) return
    const blob = new Blob([toCSV(data.items)], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `records_page${page}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div>
      <h1 className="page-title">元数据表</h1>
      <p className="page-sub">{data ? `${data.total} 条记录` : '—'}</p>

      <form
        className="toolbar"
        onSubmit={(e) => {
          e.preventDefault()
          setQ(qInput.trim())
          setPage(1)
        }}
      >
        <input
          placeholder="搜索…"
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
          style={{ minWidth: 240 }}
        />
        <select value={status} onChange={(e) => { setStatus(e.target.value); setPage(1) }}>
          <option value="">全部状态</option>
          <option value="downloaded">downloaded</option>
          <option value="failed">failed</option>
          <option value="skipped">skipped</option>
        </select>
        <button className="btn btn-primary" type="submit">搜索</button>
        <button className="btn btn-ghost" type="button" onClick={exportCSV} disabled={!data?.items.length}>
          导出本页 CSV
        </button>
      </form>

      {isLoading ? (
        <p className="muted">加载中…</p>
      ) : !data || data.items.length === 0 ? (
        <p className="empty">没有匹配的记录。</p>
      ) : (
        <div className="panel" style={{ padding: 0, overflow: 'auto' }}>
          <table>
            <thead>
              <tr>
                {COLUMNS.map((c) => (
                  <th key={c.key} onClick={() => c.sortable && toggleSort(c.key)}>
                    {c.label}
                    {sort === c.key ? (order === 'asc' ? ' ▲' : ' ▼') : ''}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.items.map((r) => (
                <tr key={r.id}>
                  <td>{r.sequence ?? '—'}</td>
                  <td>{statusBadge(r.status)}</td>
                  <td title={r.title ?? ''} style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {r.title || '—'}
                  </td>
                  <td>{r.source_site}</td>
                  <td>{r.source_item_id || '—'}</td>
                  <td style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.file_name}</td>
                  <td>{r.file_size ? `${(r.file_size / 1024).toFixed(0)} KB` : '—'}</td>
                  <td>{r.downloaded_at || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data && (
        <div className="pager">
          <button className="btn btn-ghost" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            上一页
          </button>
          <span className="chip">{page} / {totalPages}</span>
          <button className="btn btn-ghost" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
            下一页
          </button>
        </div>
      )}
    </div>
  )
}
