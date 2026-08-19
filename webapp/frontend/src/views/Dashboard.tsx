import { useQuery } from '@tanstack/react-query'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '../api'

const STATUS_COLORS: Record<string, string> = {
  downloaded: '#66bb6a',
  failed: '#ef5350',
  skipped: '#ffa726',
}
const SITE_COLORS = ['#5b9dff', '#26a69a', '#ab7df6', '#ff7597', '#ffca28', '#42a5f5']

function fmtBytes(n: number): string {
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(1)} ${units[i]}`
}

export default function Dashboard() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['stats'],
    queryFn: api.stats,
    refetchInterval: 5000,
  })

  if (isLoading) return <p className="muted">加载中…</p>
  if (isError || !data) return <p className="muted">无法读取统计数据。</p>

  const pct = data.target ? Math.min(100, (data.downloaded / data.target) * 100) : 0
  const progress = data.progress

  return (
    <div>
      <h1 className="page-title">仪表盘</h1>
      <p className="page-sub">
        关键词：<b>{data.keyword || '—'}</b>
        {!data.db_available && <span className="chip" style={{ marginLeft: 10 }}>数据库未生成</span>}
      </p>

      <div className="cards">
        <div className="card">
          <div className="k">已下载</div>
          <div className="v">
            {data.downloaded}
            {data.target ? <small> / {data.target}</small> : null}
          </div>
        </div>
        <div className="card">
          <div className="k">目标完成度</div>
          <div className="v">{pct.toFixed(1)}<small>%</small></div>
        </div>
        <div className="card">
          <div className="k">累计文件大小</div>
          <div className="v" style={{ fontSize: 22 }}>{fmtBytes(data.total_file_size)}</div>
        </div>
        <div className="card">
          <div className="k">孤儿图片</div>
          <div className="v">{data.orphan_count}</div>
        </div>
      </div>

      <div className="panel">
        <h3>目标进度</h3>
        <div className="progress-bar">
          <div className="progress-fill" style={{ width: `${pct}%` }} />
        </div>
        <p className="muted" style={{ marginTop: 10, fontSize: 13 }}>
          {data.downloaded} / {data.target ?? '∞'}（{pct.toFixed(1)}%）
          {progress && (
            <>
              {' · '}断点：第 {progress.next_page ?? progress.current_page ?? '—'} 页
              {progress.next_index != null ? ` / item ${progress.next_index}` : ''}
              {progress.remaining_records != null ? ` · 剩余 ${progress.remaining_records}` : ''}
            </>
          )}
        </p>
      </div>

      <div className="row" style={{ alignItems: 'stretch', gap: 20 }}>
        <div className="panel" style={{ flex: 1, marginBottom: 0 }}>
          <h3>按状态分布</h3>
          {data.by_status.length === 0 ? (
            <p className="empty">暂无数据</p>
          ) : (
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={data.by_status}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2a2f3a" />
                <XAxis dataKey="status" stroke="#9aa3b2" fontSize={12} />
                <YAxis stroke="#9aa3b2" fontSize={12} allowDecimals={false} />
                <Tooltip contentStyle={{ background: '#1e222b', border: '1px solid #2a2f3a', borderRadius: 8 }} />
                <Bar dataKey="count" radius={[6, 6, 0, 0]}>
                  {data.by_status.map((d) => (
                    <Cell key={d.status} fill={STATUS_COLORS[d.status] ?? '#5b9dff'} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>

        <div className="panel" style={{ flex: 1, marginBottom: 0 }}>
          <h3>按来源站点</h3>
          {data.by_source_site.length === 0 ? (
            <p className="empty">暂无数据</p>
          ) : (
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={data.by_source_site}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2a2f3a" />
                <XAxis dataKey="source_site" stroke="#9aa3b2" fontSize={11} />
                <YAxis stroke="#9aa3b2" fontSize={12} allowDecimals={false} />
                <Tooltip contentStyle={{ background: '#1e222b', border: '1px solid #2a2f3a', borderRadius: 8 }} />
                <Bar dataKey="count" radius={[6, 6, 0, 0]}>
                  {data.by_source_site.map((d, i) => (
                    <Cell key={d.source_site} fill={SITE_COLORS[i % SITE_COLORS.length]} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>
    </div>
  )
}
