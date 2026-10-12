import type {
  ApiErrorPayload,
  BlobValue,
  CellValue,
  DatasetsPayload,
  DeletePayload,
  InsertPayload,
  QueryResult,
  RowPayload,
  RowsPayload,
  SchemaPayload,
  ScriptPayload,
  TablesPayload,
  UpdatePayload,
} from './types'

// Under -u/--url-prefix the shell is served at "<prefix>/app/", and a bare
// "/api/" would 404: PrefixMiddleware strips the prefix before Flask sees it.
function apiBase(): string {
  const match = window.location.pathname.match(/^(.*?)\/app\/?$/)
  return (match ? match[1] : '') + '/api'
}

const BASE = apiBase()

export class ApiError extends Error {
  readonly status: number
  readonly payload: ApiErrorPayload | null

  constructor(message: string, status: number, payload: ApiErrorPayload | null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
  }
}

type ParamValue = string | number | boolean | null | undefined

interface RequestOptions {
  method?: string
  dataset?: string | null
  params?: Record<string, ParamValue>
  body?: unknown
}

function buildUrl(
  path: string,
  dataset: string | null | undefined,
  params: Record<string, ParamValue> | undefined,
): URL {
  const url = new URL(BASE + path, window.location.origin)
  if (dataset) url.searchParams.set('dataset', dataset)
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== null && value !== '') {
      url.searchParams.set(key, String(value))
    }
  }
  return url
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', dataset, params, body } = options
  const init: RequestInit = { method, headers: {} }
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' }
    init.body = JSON.stringify(body)
  }

  let response: Response
  try {
    response = await fetch(buildUrl(path, dataset, params), init)
  } catch (err) {
    throw new ApiError(
      'Cannot reach the server: ' + (err instanceof Error ? err.message : String(err)),
      0,
      null,
    )
  }

  const text = await response.text()
  let payload: ApiErrorPayload | null = null
  if (text) {
    try {
      payload = JSON.parse(text) as ApiErrorPayload
    } catch {
      payload = { error: text.slice(0, 400) }
    }
  }

  if (!response.ok) {
    throw new ApiError(
      (payload && payload.error) || response.statusText || 'Request failed',
      response.status,
      payload,
    )
  }
  return payload as T
}

export const api = {
  datasets: () => request<DatasetsPayload>('/datasets/'),

  tables: (dataset: string | null) => request<TablesPayload>('/tables/', { dataset }),

  schema: (dataset: string | null, table: string) =>
    request<SchemaPayload>(`/${encodeURIComponent(table)}/schema/`, { dataset }),

  rows: (
    dataset: string | null,
    table: string,
    params?: Record<string, ParamValue>,
  ) => request<RowsPayload>(`/${encodeURIComponent(table)}/rows/`, { dataset, params }),

  insert: (dataset: string | null, table: string, payload: RowPayload) =>
    request<InsertPayload>(`/${encodeURIComponent(table)}/rows/`, {
      method: 'POST',
      dataset,
      body: payload,
    }),

  update: (dataset: string | null, table: string, key: string, payload: RowPayload) =>
    request<UpdatePayload>(
      `/${encodeURIComponent(table)}/rows/${encodeURIComponent(key)}/`,
      { method: 'PATCH', dataset, body: payload },
    ),

  remove: (dataset: string | null, table: string, key: string) =>
    request<DeletePayload>(
      `/${encodeURIComponent(table)}/rows/${encodeURIComponent(key)}/`,
      { method: 'DELETE', dataset },
    ),

  query: (
    dataset: string | null,
    sql: string,
    params?: unknown,
    limit?: number,
    offset?: number,
  ) =>
    request<QueryResult>('/query/', {
      method: 'POST',
      dataset,
      params: { limit, offset },
      body: { sql, params },
    }),

  script: (dataset: string | null, sql: string, limit?: number) =>
    request<ScriptPayload>('/script/', {
      method: 'POST',
      dataset,
      params: { limit },
      body: { sql },
    }),
}

export function displayValue(value: CellValue): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') {
    if (typeof (value as BlobValue).b64 === 'string') return (value as BlobValue).b64
    return JSON.stringify(value)
  }
  return String(value)
}

export function isBlob(value: unknown): value is BlobValue {
  return value !== null && typeof value === 'object' && typeof (value as BlobValue).b64 === 'string'
}

export function inputValue(text: string, wasBlob: boolean): Exclude<CellValue, undefined> {
  if (text === '') return null
  if (wasBlob) return { b64: text }
  return text
}
