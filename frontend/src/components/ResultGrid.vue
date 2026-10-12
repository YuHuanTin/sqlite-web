<script setup lang="ts">
import { computed, ref } from 'vue'
import { displayValue, isBlob } from '../api'
import { hexPreview } from '../hex'
import { useCellSelection } from '../selection'
import { columnWidths, useVirtualRows } from '../virtual'
import type { CellValue, Row } from '../types'
import CellEditor from './CellEditor.vue'

const props = defineProps<{
  columns: string[]
  rows: Row[]
}>()

const viewing = ref<{ row: Row; column: string } | null>(null)

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

// Worked out once per result, not once per repaint: a pick repaints the grid on
// every mouse move.
const cells = computed(() =>
  props.rows.map((row) => props.columns.map((c) => ({ text: cellText(row[c]), cls: cellClass(row[c]) }))),
)

const widths = computed(() => columnWidths(props.columns, cells.value.map((row) => row.map((c) => c.text))))

const scroller = ref<HTMLElement | null>(null)
const { first, visible, padTop, padBottom, onScroll } = useVirtualRows(scroller, () => props.rows)

const drawn = computed(() =>
  visible.value.map((row, at) => {
    const index = first.value + at
    return { index, row, cells: cells.value[index] }
  }),
)

const { picked, copied, isPicked, onCellDown, onCellOver, clearSelection } = useCellSelection<Row>({
  columns: () => props.columns,
  rows: () => props.rows,
  text: cellText,
  enabled: () => viewing.value === null,
})
</script>

<template>
  <div class="result-grid">
    <!-- Always drawn: picking a cell must not shift the grid under the pointer. -->
    <div class="pick-bar">
      <span v-if="picked">{{ picked }} &middot; Ctrl+C</span>
      <span v-if="copied" class="badge">{{ copied }}</span>
    </div>

    <div ref="scroller" class="grid-wrap" @scroll.passive="onScroll" @click.self="clearSelection">
      <table class="grid fixed">
        <colgroup>
          <col v-for="(c, i) in columns" :key="c" :style="{ width: widths[i] }" />
        </colgroup>
        <thead>
          <tr>
            <th v-for="c in columns" :key="c" class="nosort">{{ c }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="padTop" class="spacer" :style="{ height: padTop + 'px' }">
            <td :colspan="columns.length"></td>
          </tr>
          <tr v-for="item in drawn" :key="item.index">
            <td
              v-for="(c, ci) in columns"
              :key="c"
              :class="[item.cells[ci].cls, { picked: isPicked(item.index, ci) }]"
              :title="item.cells[ci].text + '  (double-click)'"
              @mousedown="onCellDown(item.index, ci, $event)"
              @mouseenter="onCellOver(item.index, ci)"
              @dblclick="viewing = { row: item.row, column: c }"
            >
              {{ item.cells[ci].text }}
            </td>
          </tr>
          <tr v-if="padBottom" class="spacer" :style="{ height: padBottom + 'px' }">
            <td :colspan="columns.length"></td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length" class="empty">no rows</td>
          </tr>
        </tbody>
      </table>
    </div>

    <CellEditor
      v-if="viewing"
      :column="viewing.column"
      :value="viewing.row[viewing.column]"
      :row-key="null"
      :can-write="false"
      @close="viewing = null"
    />
  </div>
</template>
