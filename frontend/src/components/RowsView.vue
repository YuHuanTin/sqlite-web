<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api, displayValue, isBlob } from '../api'
import { confirmAction } from '../dialog'
import { hexPreview } from '../hex'
import { useCellSelection } from '../selection'
import { selectUsing } from '../sql'
import { errorText, openSql, state } from '../store'
import type { CellValue, ColumnInfo, Row, RowWithKey, RowsPayload, SchemaPayload } from '../types'
import { columnWidths, useVirtualRows } from '../virtual'
import CellEditor from './CellEditor.vue'
import RowEditor from './RowEditor.vue'

const props = defineProps<{ table: string }>()

const LIMITS = [25, 50, 100, 500]

const rows = ref<RowWithKey[]>([])
const columns = ref<string[]>([])
const schema = ref<SchemaPayload | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)

const limit = ref(50)
const offset = ref(0)
const hasNext = ref(false)
const order = ref('')
const filters = reactive<Record<string, string>>({})

type Editing =
  | { mode: 'insert'; row: null; key: null }
  | { mode: 'edit'; row: RowWithKey; key: string }

const editing = ref<Editing | null>(null)

const viewing = ref<{ row: RowWithKey; column: string } | null>(null)
const recordMenu = ref<{ x: number; y: number; row: number; col: number } | null>(null)

const {
  picked, pickedWhole, pickedRows, copied, isPicked, selectCell, onCellDown, onCellOver,
  clearSelection,
} = useCellSelection<RowWithKey>({
  columns: () => columns.value,
  rows: () => rows.value,
  text: cellText,
  enabled: () => viewing.value === null && editing.value === null,
})

function onCellMenu(row: number, col: number, event: MouseEvent): void {
  if (!canEditRows.value) return
  if (!isPicked(row, col)) selectCell(row, col)
  recordMenu.value = {
    x: Math.min(event.clientX, window.innerWidth - 170),
    y: Math.min(event.clientY, window.innerHeight - 90),
    row,
    col,
  }
}

function closeRecordMenu(): void {
  recordMenu.value = null
}

function usingThisSelect(): void {
  const at = recordMenu.value
  closeRecordMenu()
  if (!at) return
  const column = columns.value[at.col]
  const row = rows.value[at.row]
  if (column === undefined || !row) return
  openSql(selectUsing(props.table, column, row[column]))
}

function editPicked(): void {
  const row = pickedRows.value[0]
  closeRecordMenu()
  if (row) startEdit(row)
}

function deletePicked(): void {
  const chosen = pickedRows.value.filter((row) => row._key)
  closeRecordMenu()
  removeRows(chosen)
}

function openCell(row: RowWithKey, rowIndex: number, column: string): void {
  selectCell(rowIndex, columns.value.indexOf(column))
  viewing.value = { row, column }
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') closeRecordMenu()
}

const canWrite = computed(() => !state.readonly)
const hasPk = computed(() => Boolean(schema.value?.has_usable_pk))
const isView = computed(() => Boolean(schema.value?.is_view))
const canEditRows = computed(() => canWrite.value && hasPk.value)

function activeFilters(): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [key, value] of Object.entries(filters)) {
    if (value !== '') out[key] = value
  }
  return out
}

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  clearSelection()
  try {
    const [page, sch]: [RowsPayload, SchemaPayload] = await Promise.all([
      api.rows(state.dataset, props.table, {
        limit: limit.value,
        offset: offset.value,
        order: order.value || undefined,
        ...activeFilters(),
      }),
      api.schema(state.dataset, props.table),
    ])
    rows.value = page.rows
    hasNext.value = page.has_next
    schema.value = sch
    // From `page.columns`, never from a row's own keys: Flask sorts JSON object
    // keys, so those would come back in alphabetical order.
    columns.value = page.columns.length
      ? page.columns
      : sch.columns.map((c) => c.field || c.column)
  } catch (err) {
    error.value = errorText(err)
    rows.value = []
    hasNext.value = false
  } finally {
    loading.value = false
  }
}

const columnInfo = computed<Record<string, ColumnInfo>>(() => {
  const map: Record<string, ColumnInfo> = {}
  for (const c of schema.value?.columns ?? []) {
    map[c.field || c.column] = c
  }
  return map
})

function toggleOrder(name: string): void {
  const descending = '-' + name
  if (order.value === name) order.value = descending
  else if (order.value === descending) order.value = ''
  else order.value = name
  offset.value = 0
  load()
}

function sortMark(name: string): string {
  if (order.value === name) return ' ▲'
  if (order.value === '-' + name) return ' ▼'
  return ''
}

function applyFilters(): void {
  offset.value = 0
  load()
}

function clearFilters(): void {
  for (const key of Object.keys(filters)) filters[key] = ''
  applyFilters()
}

function onLimitChange(event: Event): void {
  setLimit(Number((event.target as HTMLSelectElement).value))
}

function setLimit(value: number): void {
  limit.value = value
  offset.value = 0
  load()
}

function nextPage(): void {
  offset.value += limit.value
  load()
}

function prevPage(): void {
  offset.value = Math.max(0, offset.value - limit.value)
  load()
}

function startInsert(): void {
  editing.value = { mode: 'insert', row: null, key: null }
}

function startEdit(row: RowWithKey): void {
  if (!row._key) return
  editing.value = { mode: 'edit', row, key: row._key }
}

function cellClass(value: CellValue): string {
  if (value === null || value === undefined) return 'null'
  if (isBlob(value)) return 'mono'
  return typeof value === 'number' ? 'mono' : ''
}

function cellText(value: CellValue): string {
  if (value === null || value === undefined) return 'NULL'
  if (isBlob(value)) return hexPreview(value.b64)
  return displayValue(value)
}

// Worked out once per page, not once per repaint: a pick repaints the grid on
// every mouse move.
const cells = computed(() =>
  rows.value.map((row) => columns.value.map((c) => ({ text: cellText(row[c]), cls: cellClass(row[c]) }))),
)

const widths = computed(() => columnWidths(columns.value, cells.value.map((row) => row.map((c) => c.text))))

const scroller = ref<HTMLElement | null>(null)
const { first, visible, padTop, padBottom, onScroll } = useVirtualRows(scroller, () => rows.value)

const drawn = computed(() =>
  visible.value.map((row, at) => {
    const index = first.value + at
    return { index, row, cells: cells.value[index] }
  }),
)

async function removeRows(chosen: RowWithKey[]): Promise<void> {
  if (!chosen.length) return
  const ok = await confirmAction({
    title: chosen.length === 1
      ? 'Delete this record?'
      : `Delete these ${chosen.length} records?`,
    message: chosen.length === 1
      ? 'This cannot be undone.'
      : 'This cannot be undone. They go one at a time, and a failure leaves the rest alone.',
    detail: chosen.length === 1 ? JSON.stringify(preview(chosen[0]), null, 2) : '',
    confirmLabel: chosen.length === 1 ? 'Delete' : `Delete ${chosen.length}`,
    danger: true,
  })
  if (!ok) return

  const empties = chosen.length >= rows.value.length

  error.value = null
  try {
    for (const row of chosen) {
      if (row._key) await api.remove(state.dataset, props.table, row._key)
    }
  } catch (err) {
    error.value = errorText(err)
  } finally {
    if (empties && offset.value > 0) offset.value = Math.max(0, offset.value - limit.value)
    await load()
  }
}

function preview(row: RowWithKey): Row {
  const out: Row = {}
  for (const [k, v] of Object.entries(row)) {
    if (k === '_key') continue
    out[k] = v
  }
  return out
}

function onSaved(): void {
  editing.value = null
  load()
}

function onCellSaved(): void {
  viewing.value = null
  load()
}

const hasActiveFilters = computed(() => Object.keys(activeFilters()).length > 0)

onMounted(() => {
  load()
  window.addEventListener('click', closeRecordMenu)
  window.addEventListener('keydown', onKeydown)
})

onUnmounted(() => {
  window.removeEventListener('click', closeRecordMenu)
  window.removeEventListener('keydown', onKeydown)
})

defineExpose({ load })
</script>

<template>
  <div class="rows-view">
    <div v-if="error" class="error">{{ error }}</div>

    <div class="topbar">
      <button v-if="canWrite" class="btn primary" @click="startInsert">
        + Row
      </button>
      <span v-if="isView" class="badge view">view</span>
      <span v-if="!hasPk && !isView" class="badge" title="rows cannot be addressed individually">
        no primary key
      </span>
      <span v-if="hasActiveFilters" class="badge">
        filtered
        <a href="#" @click.prevent="clearFilters">clear</a>
      </span>

      <button class="btn" :disabled="offset === 0 || loading" @click="prevPage">Previous</button>
      <button class="btn" :disabled="!hasNext || loading" @click="nextPage">Next</button>
      <span style="color: var(--muted); font-size: 12px">
        rows {{ offset + 1 }}&ndash;{{ offset + rows.length }}
      </span>

      <div class="spacer"></div>
      <span v-if="picked" style="color: var(--muted); font-size: 12px">
        {{ picked }} &middot; Ctrl+C
      </span>
      <span v-if="copied" class="badge">{{ copied }}</span>
      <label style="color: var(--muted); font-size: 12px">
        rows
        <select class="select" :value="limit" @change="onLimitChange">
          <option v-for="n in LIMITS" :key="n" :value="n">{{ n }}</option>
        </select>
      </label>
    </div>

    <div ref="scroller" class="grid-wrap" @scroll.passive="onScroll" @click.self="clearSelection">
      <table class="grid fixed">
        <colgroup>
          <col v-for="(name, i) in columns" :key="name" :style="{ width: widths[i] }" />
        </colgroup>
        <thead>
          <tr>
            <th
              v-for="name in columns"
              :key="name"
              @click="toggleOrder(name)"
              :title="columnInfo[name] ? columnInfo[name].full_type ?? '' : ''"
            >
              <span v-if="columnInfo[name]?.primary_key" class="pk">{{ name }}</span>
              <template v-else>{{ name }}</template>
              <span class="sort-mark">{{ sortMark(name) }}</span>
            </th>
          </tr>
          <tr class="filter-row">
            <th v-for="name in columns" :key="name">
              <input
                v-model="filters[name]"
                placeholder="="
                @keyup.enter="applyFilters"
                @change="applyFilters"
              />
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="padTop" class="spacer" :style="{ height: padTop + 'px' }">
            <td :colspan="columns.length"></td>
          </tr>
          <tr v-for="item in drawn" :key="item.index">
            <td
              v-for="(name, ci) in columns"
              :key="name"
              :class="[item.cells[ci].cls, { picked: isPicked(item.index, ci) }]"
              :title="item.cells[ci].text + '  (double-click)'"
              @mousedown="onCellDown(item.index, ci, $event)"
              @mouseenter="onCellOver(item.index, ci)"
              @dblclick="openCell(item.row, item.index, name)"
              @contextmenu.prevent="onCellMenu(item.index, ci, $event)"
            >
              {{ item.cells[ci].text }}
            </td>
          </tr>
          <tr v-if="padBottom" class="spacer" :style="{ height: padBottom + 'px' }">
            <td :colspan="columns.length"></td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length" class="empty">
              {{ loading ? 'Loading...' : 'No rows.' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div
      v-if="recordMenu"
      class="menu"
      :style="{ left: recordMenu.x + 'px', top: recordMenu.y + 'px' }"
    >
      <template v-if="!pickedWhole">
        <button @click="usingThisSelect">Using this Select</button>
        <button @click="editPicked">Edit Record</button>
      </template>
      <button class="danger" @click="deletePicked">
        Del Record<template v-if="pickedWhole"> ({{ pickedRows.length }})</template>
      </button>
    </div>

    <CellEditor
      v-if="viewing"
      :table="table"
      :column="viewing.column"
      :value="viewing.row[viewing.column]"
      :row-key="viewing.row._key ?? null"
      :can-write="canWrite && !isView"
      :info="columnInfo[viewing.column] ?? null"
      @close="viewing = null"
      @saved="onCellSaved"
    />

    <RowEditor
      v-if="editing && schema"
      :table="table"
      :schema="schema"
      :mode="editing.mode"
      :row="editing.row"
      :row-key="editing.key"
      @close="editing = null"
      @saved="onSaved"
    />
  </div>
</template>
