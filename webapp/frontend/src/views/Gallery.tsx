import { useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { api } from '../api'
import type { ImageRecord } from '../types'

const PAGE_SIZE = 60

function MetaRow({ k, v }: { k: string; v: React.ReactNode }) {
  if (v === null || v === undefined || v === '') return null
  return (
    <div className="meta-row">
      <span className="mk">{k}</span>
      <span className="mv">{v}</span>
    </div>
  )
}

export default function Gallery() {
  const [page, setPage] = useState(1)
  const [q, setQ] = useState('')
  const [qInput, setQInput] = useState('')
  const [status, setStatus] = useState('')
  const [active, setActive] = useState<ImageRecord | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['gallery', page, q, status],
    queryFn: () => api.images({ page, page_size: PAGE_SIZE, q, status, sort: 'sequence', order: 'asc' }),
    placeholderData: keepPreviousData,
  })

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1

  return (
    <div>
      <h1 className="page-title">图片画廊</h1>
      <p className="page-sub">{data ? `${data.total} 张图片` : '—'}</p>

      <form
        className="toolbar"
        onSubmit={(e) => {
          e.preventDefault()
          setQ(qInput.trim())
          setPage(1)
        }}
      >
        <input
          placeholder="搜索标题 / 关键词 / item id…"
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
          style={{ minWidth: 280 }}
        />
        <select value={status} onChange={(e) => { setStatus(e.target.value); setPage(1) }}>
          <option value="">全部状态</option>
          <option value="downloaded">downloaded</option>
          <option value="failed">failed</option>
          <option value="skipped">skipped</option>
        </select>
        <button className="btn btn-primary" type="submit">搜索</button>
      </form>

      {isLoading ? (
        <p className="muted">加载中…</p>
      ) : !data || data.items.length === 0 ? (
        <p className="empty">没有匹配的图片。</p>
      ) : (
        <div className="grid">
          {data.items.map((img) => (
            <div key={img.id} className="tile" onClick={() => setActive(img)}>
              <img loading="lazy" src={api.imageFileUrl(img.id)} alt={img.title ?? img.file_name} />
              <div className="cap">
                <span className="seq">#{img.sequence ?? '—'}</span>{' '}
                {img.title || img.file_name}
              </div>
            </div>
          ))}
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

      {active && (
        <div className="modal-mask" onClick={() => setActive(null)}>
          <button className="modal-close" onClick={() => setActive(null)}>×</button>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <img src={api.imageFileUrl(active.id)} alt={active.title ?? active.file_name} />
            <div className="meta">
              <h3>{active.title || active.file_name}</h3>
              <MetaRow k="序号" v={active.sequence} />
              <MetaRow k="状态" v={active.status} />
              <MetaRow k="来源站" v={active.source_site} />
              <MetaRow k="item id" v={active.source_item_id} />
              <MetaRow k="合集" v={active.collection_title} />
              <MetaRow k="文件名" v={active.file_name} />
              <MetaRow k="尺寸" v={active.width && active.height ? `${active.width}×${active.height}` : null} />
              <MetaRow k="大小" v={active.file_size ? `${(active.file_size / 1024).toFixed(1)} KB` : null} />
              <MetaRow k="页面" v={active.page_url ? <a href={active.page_url} target="_blank" rel="noreferrer">{active.page_url}</a> : null} />
              <MetaRow k="图片URL" v={active.image_url ? <a href={active.image_url} target="_blank" rel="noreferrer">{active.image_url}</a> : null} />
              <MetaRow k="摘要" v={active.summary} />
              <MetaRow k="下载时间" v={active.downloaded_at} />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
