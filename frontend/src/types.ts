export interface BlobValue {
  b64: string
}

/** `undefined` is in here because a row only carries the columns the query projected. */
export type CellValue = string | number | boolean | null | BlobValue | undefined

export type Row = Record<string, CellValue>

export type RowWithKey = Row & { _key?: string }

/** A write payload: no `undefined`, so JSON.stringify cannot silently drop a key. */
export type RowPayload = Record<string, Exclude<CellValue, undefined>>

export interface Dataset {
  key: string
  basename: string
  is_readonly: boolean
}

export interface DatasetsPayload {
  datasets: Dataset[]
}

export interface TableInfo {
  name: string
  is_view: boolean
  has_usable_pk: boolean
  count: number | null
}

export interface TablesPayload {
  tables: TableInfo[]
  is_readonly: boolean
}

export interface ColumnInfo {
  column: string
  /** The peewee name the row endpoints speak; null if reflection dropped it. */
  field: string | null
  type: string
  full_type: string | null
  null: boolean
  default: string | null
  primary_key: boolean
  autoincrement: boolean
}

export type PrimaryKey =
  | { composite: true; fields: string[] }
  | { composite: false; field: string; column: string }
  | null

export interface IndexInfo {
  name: string
  sql: string | null
  columns: string[]
  unique: boolean
}

export interface ForeignKeyInfo {
  column: string
  dest_table: string
  dest_column: string
  on_delete: string | null
  on_update: string | null
}

export interface SchemaPayload {
  table: string
  sql: string
  columns: ColumnInfo[]
  primary_key: PrimaryKey
  has_usable_pk: boolean
  is_view: boolean
  is_readonly: boolean
  indexes: IndexInfo[]
  foreign_keys: ForeignKeyInfo[]
  triggers: string[]
}

export interface RowsPayload {
  table: string
  columns: string[]
  rows: RowWithKey[]
  limit: number
  offset: number
  has_next: boolean
  next_offset: number | null
  is_readonly: boolean
}

export interface RowsResult {
  kind: 'rows'
  sql: string
  columns: string[]
  rows: RowWithKey[]
  has_next: boolean
  /** Only POST /query/ sends these; a statement inside a script has no page. */
  limit?: number
  offset?: number
  next_offset?: number | null
  /**
   * The table a delete could follow this statement from, or null. The server's
   * own reading of the statement, so the console does not have to guess. The
   * rows carry a `_key` when it is set and the projection kept the key columns.
   */
  delete_target?: string | null
}

export interface AffectedResult {
  kind: 'affected'
  sql: string
  /** -1 for DDL, which SQLite does not count. */
  affected: number
}

export interface ErrorResult {
  kind: 'error'
  sql: string
  error: string
}

export type QueryResult = RowsResult | AffectedResult
export type StatementResult = QueryResult | ErrorResult

export interface ScriptPayload {
  results: StatementResult[]
  failed: boolean
  total_statements: number
}

export interface InsertPayload {
  inserted: number
  /** Present only for a single auto-increment integer key. */
  key?: string
}

export interface UpdatePayload {
  updated: number
}

export interface DeletePayload {
  deleted: number
}

/** Every error body. ApiError's `extra` is merged in alongside "error". */
export interface ApiErrorPayload {
  error: string
  [key: string]: unknown
}
