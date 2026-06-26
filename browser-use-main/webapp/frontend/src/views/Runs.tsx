import { useQuery } from '@tanstack/react-query'
import { api } from '../api'

export default function Runs() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['runs'],
    queryFn: api.runs,
    refetchInterval: 8000,
  })

  if (isLoading) return <p className="muted">加载中…</p>
  if (isError) return <p className="muted">无法读取运行历史（数据库可能未生成）。</p>

  return (
    <div>
      <h1 className="page-title">运行历史</h1>
      <p className="page-sub">每次导入到 SQLite 的批次记录</p>

      {!data || data.length === 0 ? (
        <p className="empty">暂无导入批次。</p>
      ) : (
        <div className="panel" style={{ padding: 0, overflow: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th>批次 ID</th>
                <th>状态</th>
                <th>开始</th>
                <th>完成</th>
                <th>总数</th>
                <th>导入</th>
                <th>跳过</th>
                <th>错误</th>
                <th>孤儿</th>
              </tr>
            </thead>
            <tbody>
              {data.map((r) => (
                <tr key={r.id}>
                  <td style={{ fontFamily: 'monospace', fontSize: 12 }}>{r.id}</td>
                  <td>{r.status}</td>
                  <td>{r.started_at || '—'}</td>
                  <td>{r.completed_at || '—'}</td>
                  <td>{r.total_records}</td>
                  <td>{r.imported_count}</td>
                  <td>{r.skipped_count}</td>
                  <td>{r.error_count}</td>
                  <td>{r.orphan_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
