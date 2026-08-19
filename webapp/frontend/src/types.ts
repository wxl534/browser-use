// 与后端 catalog.py / app.py 的返回结构对齐的前端类型。

export interface ImageRecord {
  id: string
  source_site: string
  source_item_id: string | null
  sequence: number | null
  status: string
  title: string | null
  collection_title: string | null
  page_url: string | null
  image_url: string | null
  file_name: string
  file_path: string
  sha256: string
  file_size: number
  width: number | null
  height: number | null
  evidence: string | null
  metadata_text: string | null
  summary: string | null
  recorded_at: string | null
  downloaded_at: string | null
  imported_at: string | null
  updated_at: string | null
  import_run_id: string
}

export interface ImagePage {
  items: ImageRecord[]
  total: number
  page: number
  page_size: number
}

export interface Progress {
  next_page?: number
  next_index?: number
  current_page?: number
  remaining_records?: number
  [key: string]: unknown
}

export interface Stats {
  downloaded: number
  target: number | null
  keyword: string
  db_available: boolean
  by_status: { status: string; count: number }[]
  by_source_site: { source_site: string; count: number }[]
  total_file_size: number
  orphan_count: number
  progress: Progress | null
}

export interface ImportRun {
  id: string
  started_at: string
  completed_at: string | null
  status: string
  total_records: number
  imported_count: number
  skipped_count: number
  error_count: number
  orphan_count: number
}

export interface RunStatus {
  running: boolean
  pid: number | null
  params: Record<string, unknown> | null
  last_exit_code: number | null
  llm_blocked_exit_code: number
}

export interface RunParams {
  keyword: string
  target: number
  mode: 'new' | 'resume'
  crawl_mode?: 'idp_batch' | 'generic_per_item'
  max_rounds: number
  round_timeout: number
  max_no_progress: number
  sleep_seconds: number
  page_delay: number
  cooldown: number
  concurrency: number
  api_key?: string
  base_url?: string
  chrome_exe?: string
  chrome_user_data?: string
  chrome_profile?: string
  proxy_server?: string
  scrapling_py?: string
  cf_url?: string
  storage_state?: string
}
