import type { ImagePage, ImageRecord, ImportRun, RunParams, RunStatus, Stats } from './types'

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}

async function postJSON<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error((data as { error?: string }).error || `${res.status} ${res.statusText}`)
  return data as T
}

export const api = {
  stats: () => getJSON<Stats>('/api/stats'),
  images: (params: {
    page: number
    page_size: number
    q?: string
    status?: string
    sort?: string
    order?: string
  }) => {
    const qs = new URLSearchParams({
      page: String(params.page),
      page_size: String(params.page_size),
      q: params.q ?? '',
      status: params.status ?? '',
      sort: params.sort ?? 'sequence',
      order: params.order ?? 'asc',
    })
    return getJSON<ImagePage>(`/api/images?${qs.toString()}`)
  },
  imageDetail: (id: string) => getJSON<ImageRecord>(`/api/images/${id}`),
  imageFileUrl: (id: string) => `/api/images/${id}/file`,
  runs: () => getJSON<ImportRun[]>('/api/runs'),
  runStatus: () => getJSON<RunStatus>('/api/run/status'),
  startRun: (params: RunParams) => postJSON<{ ok: boolean; status: RunStatus }>('/api/run/start', params),
  stopRun: () => postJSON<{ ok: boolean }>('/api/run/stop', undefined),
  recentLogs: () => getJSON<{ lines: string[] }>('/api/logs/recent'),
}
