import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import type { RunParams } from '../types'

const DEFAULTS: RunParams = {
  keyword: '',
  target: 5000,
  mode: 'new',
  crawl_mode: 'generic_per_item',
  max_rounds: 100,
  round_timeout: 0,
  max_no_progress: 3,
  sleep_seconds: 5,
  page_delay: 12,
  cooldown: 60,
  concurrency: 2,
}

const NUM_FIELDS: { key: keyof RunParams; label: string }[] = [
  { key: 'target', label: '目标数量' },
  { key: 'max_rounds', label: '最大轮数' },
  { key: 'round_timeout', label: '单轮超时(s,0=不限)' },
  { key: 'max_no_progress', label: '无进展轮数上限' },
  { key: 'sleep_seconds', label: '轮间隔(s)' },
  { key: 'page_delay', label: '翻页延迟(s)' },
  { key: 'cooldown', label: '冷却(s)' },
  { key: 'concurrency', label: '下载并发' },
]

const ENV_FIELDS: { key: keyof RunParams; label: string }[] = [
  { key: 'api_key', label: 'OPENAI_API_KEY' },
  { key: 'base_url', label: 'OPENAI_BASE_URL' },
  { key: 'chrome_exe', label: 'Chrome 可执行路径' },
  { key: 'chrome_user_data', label: 'Chrome 用户数据目录' },
  { key: 'chrome_profile', label: 'Chrome Profile' },
  { key: 'proxy_server', label: '代理服务器' },
  { key: 'scrapling_py', label: 'Scrapling Python' },
  { key: 'cf_url', label: 'Cloudflare 取证 URL' },
  { key: 'storage_state', label: 'Storage State 文件' },
]

function lineClass(s: string): string {
  const low = s.toLowerCase()
  if (s.startsWith('====') || s.startsWith('----')) return 'ln-sep'
  if (low.includes('error') || low.includes('失败') || low.includes('traceback')) return 'ln-err'
  if (low.includes('成功') || low.includes('completed') || low.includes('done')) return 'ln-ok'
  return ''
}

export default function Control() {
  const qc = useQueryClient()
  const [form, setForm] = useState<RunParams>(DEFAULTS)
  const [lines, setLines] = useState<string[]>([])
  const [showEnv, setShowEnv] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const consoleRef = useRef<HTMLDivElement>(null)

  const status = useQuery({ queryKey: ['run-status'], queryFn: api.runStatus, refetchInterval: 3000 })
  const running = status.data?.running ?? false

  useEffect(() => {
    const es = new EventSource('/api/logs/stream')
    es.onmessage = (e) => {
      try {
        const line = JSON.parse(e.data) as string
        setLines((prev) => {
          const next = [...prev, line]
          return next.length > 4000 ? next.slice(next.length - 4000) : next
        })
      } catch {
        /* keep-alive / ignore */
      }
    }
    es.onerror = () => { /* EventSource auto-reconnects */ }
    return () => es.close()
  }, [])

  useEffect(() => {
    const el = consoleRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [lines])

  function setField<K extends keyof RunParams>(key: K, value: RunParams[K]) {
    setForm((f) => ({ ...f, [key]: value }))
  }

  async function start() {
    setErr('')
    if (!form.keyword.trim()) {
      setErr('请填写搜索关键词')
      return
    }
    setBusy(true)
    try {
      await api.startRun(form)
      qc.invalidateQueries({ queryKey: ['run-status'] })
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    setBusy(true)
    try {
      await api.stopRun()
      qc.invalidateQueries({ queryKey: ['run-status'] })
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <h1 className="page-title">控制台</h1>
      <p className="page-sub">
        启动 / 停止爬虫监督进程，并实时查看日志。状态：
        <b style={{ color: running ? 'var(--ok)' : 'var(--muted)' }}>{running ? ` 运行中 (pid ${status.data?.pid})` : ' 空闲'}</b>
        {status.data?.last_exit_code != null && !running && (
          <span className="chip" style={{ marginLeft: 10 }}>上次退出码 {status.data.last_exit_code}</span>
        )}
      </p>

      <div className="panel">
        <h3>运行参数</h3>
        <div className="form-grid">
          <div className="field">
            <label>搜索关键词 *</label>
            <input value={form.keyword} onChange={(e) => setField('keyword', e.target.value)} disabled={running} />
          </div>
          <div className="field">
            <label>启动模式</label>
            <select value={form.mode} onChange={(e) => setField('mode', e.target.value as RunParams['mode'])} disabled={running}>
              <option value="new">新任务 (--new-run)</option>
              <option value="resume">断点续传 (--resume)</option>
            </select>
          </div>
          <div className="field">
            <label>爬取模式</label>
            <select value={form.crawl_mode} onChange={(e) => setField('crawl_mode', e.target.value as RunParams['crawl_mode'])} disabled={running}>
              <option value="generic_per_item">generic_per_item（通用逐 item）</option>
              <option value="idp_batch">idp_batch（IDP 整页批量）</option>
            </select>
          </div>
          {NUM_FIELDS.map((f) => (
            <div className="field" key={f.key}>
              <label>{f.label}</label>
              <input
                type="number"
                value={form[f.key] as number}
                onChange={(e) => setField(f.key, Number(e.target.value) as never)}
                disabled={running}
              />
            </div>
          ))}
        </div>

        <div style={{ marginTop: 14 }}>
          <button className="btn btn-ghost" type="button" onClick={() => setShowEnv((v) => !v)}>
            {showEnv ? '收起' : '展开'}高级 / 环境变量
          </button>
        </div>
        {showEnv && (
          <div className="form-grid" style={{ marginTop: 14 }}>
            {ENV_FIELDS.map((f) => (
              <div className="field" key={f.key}>
                <label>{f.label}</label>
                <input
                  type={f.key === 'api_key' ? 'password' : 'text'}
                  value={(form[f.key] as string) ?? ''}
                  onChange={(e) => setField(f.key, e.target.value as never)}
                  disabled={running}
                />
              </div>
            ))}
          </div>
        )}

        {err && <p style={{ color: 'var(--danger)', marginTop: 12, fontSize: 13 }}>{err}</p>}

        <div className="row" style={{ marginTop: 16 }}>
          <button className="btn btn-primary" onClick={start} disabled={busy || running}>
            ▶ 启动
          </button>
          <button className="btn btn-danger" onClick={stop} disabled={busy || !running}>
            ■ 停止
          </button>
        </div>
      </div>

      <div className="panel">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <h3 style={{ margin: 0 }}>实时日志</h3>
          <button className="btn btn-ghost" type="button" onClick={() => setLines([])}>清屏</button>
        </div>
        <div className="console" ref={consoleRef} style={{ marginTop: 12 }}>
          {lines.length === 0 ? (
            <span className="muted">等待日志…</span>
          ) : (
            lines.map((l, i) => (
              <div key={i} className={lineClass(l)}>{l}</div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}
