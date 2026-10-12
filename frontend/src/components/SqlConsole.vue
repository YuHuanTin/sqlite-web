<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../api'
import { confirmAction } from '../dialog'
import { oneLine } from '../history'
import { errorText, forgetSqlHistory, markSqlDirty, rememberSql, state } from '../store'
import type { RowsResult, StatementResult } from '../types'
import ResultGrid from './ResultGrid.vue'

type Mode = 'query' | 'script'

interface RunResults {
  mode: Mode
  results: StatementResult[]
  failed: boolean
  total_statements: number
}

const props = defineProps<{
  tabId: number
  initial?: string
}>()

const sql = ref(props.initial ?? '')
const mode = ref<Mode>('query')
const limit = ref(1000)
const offset = ref(0)
// The statement the results on screen came from. Paging re-runs it: re-running
// whatever is in the box now would show a page of a different query.
const ranSql = ref('')
const busy = ref(false)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const results = ref<RunResults | null>(null)

const dirty = computed(() => sql.value.trim().length > 0)

watch(dirty, () => markSqlDirty(props.tabId, dirty.value))

const canRun = computed(() => sql.value.trim().length > 0 && !busy.value)

function summarise(result: StatementResult): string {
  if (result.kind === 'error') return result.error
  if (result.kind === 'affected') {
    return `${result.affected} row${result.affected === 1 ? '' : 's'} affected`
  }
  return `${result.rows.length} row${result.rows.length === 1 ? '' : 's'}` +
    (result.has_next ? ' (more available)' : '')
}

const summary = computed(() => {
  const run = results.value
  if (!run) return ''

  let queried = 0 // statements that returned rows, so that "0 rows" still shows
  let rows = 0
  let affected = 0
  let schema = 0
  let errors = 0
  let more = false

  for (const result of run.results) {
    if (result.kind === 'error') {
      errors += 1
    } else if (result.kind === 'affected') {
      // SQLite does not count DDL and reports -1 for it.
      if (result.affected < 0) schema += 1
      else affected += result.affected
    } else {
      queried += 1
      rows += result.rows.length
      more = more || result.has_next
    }
  }

  const parts: string[] = []
  if (queried) {
    parts.push(`${rows} row${rows === 1 ? '' : 's'}${more ? ' (more available)' : ''}`)
  }
  if (affected) parts.push(`${affected} row${affected === 1 ? '' : 's'} affected`)
  if (schema) parts.push('schema changed')
  if (errors) parts.push(`${errors} error${errors === 1 ? '' : 's'}`)
  return parts.join(' · ')
})

function pad(text: number): string {
  return String(text).padStart(3)
}

function recall(statement: string): void {
  sql.value = statement
}

function clearHistory(): void {
  forgetSqlHistory()
}

async function run(from = 0, text: string = sql.value): Promise<void> {
  busy.value = true
  error.value = null
  notice.value = null
  results.value = null
  offset.value = from
  ranSql.value = text
  // Before the run, not after: a statement that fails is the one worth having back.
  rememberSql(text)
  try {
    if (mode.value === 'script') {
      const data = await api.script(state.dataset, text, limit.value)
      results.value = { mode: 'script', ...data }
    } else {
      const data = await api.query(state.dataset, text, undefined, limit.value, from)
      results.value = { mode: 'query', results: [data], failed: false, total_statements: 1 }
    }
  } catch (err) {
    error.value = errorText(err)
  } finally {
    busy.value = false
  }
}

function nextPage(): void {
  run(offset.value + limit.value, ranSql.value)
}

function prevPage(): void {
  run(Math.max(0, offset.value - limit.value), ranSql.value)
}

const paged = computed<RowsResult | null>(() => {
  const current = results.value
  if (!current || current.mode !== 'query') return null
  const only = current.results[0]
  if (!only || only.kind !== 'rows') return null
  return offset.value > 0 || only.has_next ? only : null
})

watch(limit, () => {
  offset.value = 0
})

// Deleting follows the rows the console drew, and it goes through the same row
// endpoint the data grid uses: the server hands back one key per row, so there
// is no statement to build here and no second way to address a record. A
// projection that dropped the key columns gets no keys and cannot be deleted.
const deleteScope = computed<{ table: string; keys: string[] } | null>(() => {
  const current = results.value
  if (!current || current.mode !== 'query') return null
  const only = current.results[0]
  if (!only || only.kind !== 'rows' || !only.delete_target) return null
  const keys = only.rows.map((row) => row._key)
  if (!keys.length || keys.some((key) => !key)) return null
  return { table: only.delete_target, keys: keys as string[] }
})

const canDelete = computed(() => Boolean(deleteScope.value) && !busy.value && !state.readonly)

async function deleteFound(): Promise<void> {
  const scope = deleteScope.value
  if (!scope) return

  const many = scope.keys.length === 1 ? 'record' : 'records'
  const ok = await confirmAction({
    title: `Delete ${scope.keys.length} ${many} from "${scope.table}"?`,
    message: 'These are the records the query found. This cannot be undone.',
    confirmLabel: `Delete ${scope.keys.length}`,
    danger: true,
  })
  if (!ok) return

  busy.value = true
  error.value = null
  notice.value = null
  try {
    let removed = 0
    for (const key of scope.keys) {
      const done = await api.remove(state.dataset, scope.table, key)
      removed += done.deleted
    }
    await run(offset.value, ranSql.value)
    notice.value = `${removed} ${removed === 1 ? 'record' : 'records'} deleted.`
  } catch (err) {
    error.value = errorText(err)
  } finally {
    busy.value = false
  }
}

function clear(): void {
  sql.value = ''
  results.value = null
  error.value = null
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
    event.preventDefault()
    if (canRun.value) run()
  }
}
</script>

<template>
  <div>
    <div class="topbar" style="padding: 0 0 10px; background: none; border: none">
      <div class="tabs">
        <button class="tab" :class="{ active: mode === 'query' }" @click="mode = 'query'">
          Single statement
        </button>
        <button class="tab" :class="{ active: mode === 'script' }" @click="mode = 'script'">
          Script
        </button>
      </div>
      <span class="badge">{{ state.datasetInfo ? state.datasetInfo.basename : '' }}</span>
      <div class="spacer"></div>
      <label style="color: var(--muted); font-size: 12px">
        max rows
        <select class="select" v-model.number="limit">
          <option :value="100">100</option>
          <option :value="1000">1000</option>
          <option :value="10000">10000</option>
        </select>
      </label>
      <button class="btn" @click="clear">Clear</button>
      <button class="btn primary" :disabled="!canRun" @click="run()">
        {{ busy ? 'Running...' : 'Run' }}
      </button>
      <button
        class="btn danger-solid"
        :disabled="!canDelete"
        title="Delete the records this query found"
        @click="deleteFound"
      >
        Delete Record
      </button>
    </div>

    <div class="console-split">
      <textarea
        v-model="sql"
        class="input console-sql"
        spellcheck="false"
        placeholder="SELECT * FROM ...&#10;&#10;Ctrl/Cmd+Enter to run"
        @keydown="onKeydown"
      ></textarea>

      <aside class="history">
        <div class="history-head">
          <span>History</span>
          <button
            v-if="state.sqlHistory.length"
            class="link"
            title="Forget every entry"
            @click="clearHistory"
          >
            clear
          </button>
        </div>
        <ul>
          <li
            v-for="entry in state.sqlHistory"
            :key="entry"
            :title="entry"
            @click="recall(entry)"
          >
            {{ oneLine(entry) }}
          </li>
          <li v-if="!state.sqlHistory.length" class="empty">nothing run yet</li>
        </ul>
      </aside>
    </div>

    <div v-if="error" class="error" style="margin-top: 12px">{{ error }}</div>
    <div v-else-if="notice" class="notice" style="margin-top: 12px">{{ notice }}</div>

    <template v-if="results">
      <p style="color: var(--muted); font-size: 12px; margin: 12px 0 6px">
        {{ results.total_statements }} statement{{ results.total_statements === 1 ? '' : 's' }}
        <template v-if="summary"> &middot; {{ summary }}</template>
        <template v-if="results.failed"> &middot; stopped at the first error</template>
      </p>

      <div v-if="paged" class="pager">
        <button class="btn" :disabled="offset === 0 || busy" @click="prevPage">Previous</button>
        <button class="btn" :disabled="!paged.has_next || busy" @click="nextPage">Next</button>
        <span>rows {{ offset + 1 }}&ndash;{{ offset + paged.rows.length }}</span>
        <span v-if="paged.has_next" style="color: var(--muted); font-size: 12px">
          more available
        </span>
      </div>

      <div v-for="(result, i) in results.results" :key="i" style="margin-bottom: 14px">
        <div v-if="results.mode === 'script'" style="color: var(--muted); font-size: 12px; margin-bottom: 4px">
          <span class="mono">{{ pad(i + 1) }}</span>
          {{ summarise(result) }}
        </div>

        <div v-if="result.kind === 'error'" class="error">{{ result.error }}</div>

        <div v-else-if="result.kind === 'affected'" class="notice">
          <template v-if="result.affected < 0">schema changed</template>
          <template v-else>
            {{ result.affected }} row{{ result.affected === 1 ? '' : 's' }} affected
          </template>
        </div>

        <ResultGrid v-else :columns="result.columns" :rows="result.rows" />
      </div>
    </template>
  </div>
</template>
