import { reactive } from 'vue'
import { api, ApiError } from './api'
import { forgetAll, loadHistory, remember } from './history'
import type { Dataset, TableInfo } from './types'

const KEYS = {
  dataset: 'sqlite-web.dataset',
  table: 'sqlite-web.table',
  view: 'sqlite-web.view',
} as const

type StorageKey = (typeof KEYS)[keyof typeof KEYS]

function read(key: StorageKey): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

function write(key: StorageKey, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key)
    else window.localStorage.setItem(key, value)
  } catch {
    // Losing a convenience must never break the app.
  }
}

export type View = 'rows' | 'schema'

export const VIEWS: readonly View[] = ['rows', 'schema']

export interface TableTab {
  id: number
  kind: 'table'
  table: string
  view: View
}

export interface SqlTab {
  id: number
  kind: 'sql'
  label: string
  initial: string
  dirty: boolean
}

export type Tab = TableTab | SqlTab

interface State {
  booted: boolean
  loading: boolean
  error: string | null
  notice: string | null
  authRequired: boolean

  datasets: Dataset[]
  dataset: string | null
  datasetInfo: Dataset | null

  tables: TableInfo[]

  tabs: Tab[]
  activeId: number | null

  sqlHistory: string[]

  readonly: boolean
}

export const state = reactive<State>({
  booted: false,
  loading: false,
  error: null,
  notice: null,
  authRequired: false,

  datasets: [],
  dataset: null,
  datasetInfo: null,

  tables: [],

  tabs: [],
  activeId: null,

  sqlHistory: loadHistory(),

  readonly: false,
})

let nextTabId = 1
let consoleCount = 0

export function isAuthError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401
}

export function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    return err.status ? `${err.message} (HTTP ${err.status})` : err.message
  }
  return String(err instanceof Error ? err.message : err)
}

export function setError(err: unknown): void {
  state.error = errorText(err)
}

export function clearMessages(): void {
  state.error = null
  state.notice = null
}

export async function bootstrap(): Promise<void> {
  state.loading = true
  clearMessages()
  try {
    const { datasets } = await api.datasets()
    state.datasets = datasets
    if (!datasets.length) {
      state.dataset = null
      state.tables = []
      closeAllTabs()
      state.notice = 'No databases are loaded.'
      state.booted = true
      return
    }

    const remembered = read(KEYS.dataset)
    const found = datasets.find((d) => d.key === remembered)
    await selectDataset((found ?? datasets[0]).key, { keepTable: true })
    state.booted = true
  } catch (err) {
    if (isAuthError(err)) state.authRequired = true
    else setError(err)
  } finally {
    state.loading = false
  }
}

interface SelectOptions {
  keepTable?: boolean
}

export async function selectDataset(key: string, { keepTable = false }: SelectOptions = {}): Promise<void> {
  state.dataset = key
  write(KEYS.dataset, key)
  const info = state.datasets.find((d) => d.key === key) ?? null
  state.datasetInfo = info
  state.readonly = Boolean(info?.is_readonly)

  closeAllTabs()

  await loadTables()

  const remembered = keepTable ? read(KEYS.table) : null
  const target = state.tables.find((t) => t.name === remembered)
  const want = target ?? state.tables[0] ?? null
  if (want) openTable(want.name)
  else applyHash()
}

export async function loadTables(): Promise<void> {
  if (!state.dataset) {
    state.tables = []
    return
  }
  const data = await api.tables(state.dataset)
  state.tables = data.tables
  state.readonly = Boolean(data.is_readonly)

  const names = new Set(state.tables.map((t) => t.name))
  const gone = state.tabs.filter((t): t is TableTab => t.kind === 'table' && !names.has(t.table))
  for (const tab of gone) closeTab(tab.id)
  if (gone.length) {
    const list = gone.map((t) => `"${t.table}"`).join(', ')
    state.notice = `${list} ${gone.length === 1 ? 'is' : 'are'} no longer there.`
  }
}

export function activeTab(): Tab | null {
  return state.tabs.find((t) => t.id === state.activeId) ?? null
}

export function activeTableTab(): TableTab | null {
  const tab = activeTab()
  return tab && tab.kind === 'table' ? tab : null
}

function show(tab: Tab): void {
  state.activeId = tab.id
  applyHash()
}

function closeAllTabs(): void {
  state.tabs = []
  state.activeId = null
}

// Bootstrapping the dataset opens a default table; a deep link has to replace
// that rather than sit beside it, so tabs opened while initialising start empty.
let initialising = true

export function openTable(name: string, view?: View): void {
  if (initialising) closeAllTabs()

  const existing = state.tabs.find((t): t is TableTab => t.kind === 'table' && t.table === name)
  if (existing) {
    if (view) existing.view = view
    show(existing)
    return
  }

  const remembered = read(KEYS.view)
  const first = view ?? (VIEWS.includes(remembered as View) ? (remembered as View) : 'rows')
  const tab: TableTab = { id: nextTabId++, kind: 'table', table: name, view: first }
  state.tabs.push(tab)
  write(KEYS.table, name)
  write(KEYS.view, first)
  show(tab)
}

export function openSql(initial = ''): void {
  if (initialising) closeAllTabs()
  consoleCount += 1
  const tab: SqlTab = {
    id: nextTabId++,
    kind: 'sql',
    label: `SQL ${consoleCount}`,
    initial,
    dirty: initial.trim().length > 0,
  }
  state.tabs.push(tab)
  show(tab)
}

export function rememberSql(sql: string): void {
  state.sqlHistory = remember(sql, state.sqlHistory)
}

export function forgetSqlHistory(): void {
  state.sqlHistory = forgetAll()
}

export function markSqlDirty(id: number, dirty: boolean): void {
  const tab = state.tabs.find((t) => t.id === id)
  if (tab && tab.kind === 'sql' && tab.dirty !== dirty) tab.dirty = dirty
}

export function activateTab(id: number): void {
  const tab = state.tabs.find((t) => t.id === id)
  if (tab) show(tab)
}

export function closeTab(id: number): void {
  const at = state.tabs.findIndex((t) => t.id === id)
  if (at === -1) return
  const [gone] = state.tabs.splice(at, 1)
  if (gone.id !== state.activeId) return
  const next = state.tabs[at] ?? state.tabs[at - 1] ?? null
  state.activeId = next ? next.id : null
  applyHash()
}

export function setView(view: View): void {
  const tab = activeTableTab()
  if (!tab) return
  tab.view = view
  write(KEYS.view, view)
  applyHash()
}

export function hashFor(): string {
  if (!state.tabs.length) return '#/'
  const tab = activeTableTab()
  if (!tab) return '#/sql'
  return `#/${encodeURIComponent(tab.table)}/${tab.view}`
}

export function applyHash(): void {
  const want = hashFor()
  if (window.location.hash !== want) {
    window.history.replaceState(null, '', want)
  }
}

function openFromHash(raw: string): void {
  if (!raw) return
  if (raw === 'sql') {
    openSql()
    return
  }
  const [name, view] = raw.split('/')
  const table = decodeURIComponent(name ?? '')
  if (!state.tables.some((t) => t.name === table)) {
    state.notice = `Table "${table}" is not in this database.`
    return
  }
  openTable(table, VIEWS.includes(view as View) ? (view as View) : undefined)
}

// Read before anything can rewrite it: selectDataset() calls applyHash(), so
// reading window.location.hash later returns the default table bootstrap wrote.
const initialHash = window.location.hash

export function restoreFromHash(): void {
  try {
    openFromHash(initialHash.replace(/^#\/?/, ''))
  } finally {
    initialising = false
  }
}

export function applyLocationHash(): void {
  openFromHash(window.location.hash.replace(/^#\/?/, ''))
}
