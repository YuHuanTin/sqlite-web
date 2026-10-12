import { computed, onMounted, onUnmounted, ref, type ComputedRef, type Ref } from 'vue'
import type { CellValue, Row } from './types'

export interface Cell {
  row: number
  col: number
}

export interface Bounds {
  r0: number
  r1: number
  c0: number
  c1: number
  whole: boolean
}

interface SelectionOptions<T extends Row> {
  columns: () => string[]
  rows: () => T[]
  text: (value: CellValue) => string
  enabled?: () => boolean
}

export interface CellSelection<T extends Row> {
  picked: ComputedRef<string>
  pickedWhole: ComputedRef<boolean>
  pickedRows: ComputedRef<T[]>
  copied: Ref<string>
  isPicked: (row: number, col: number) => boolean
  selectCell: (row: number, col: number) => void
  onCellDown: (row: number, col: number, event: MouseEvent) => void
  onCellOver: (row: number, col: number) => void
  clearSelection: () => void
  copySelection: () => Promise<void>
}

// Only one grid may hold a pick at a time. A console can show several result
// grids at once and every one of them registers the same Ctrl+C, so without this
// a pick in two of them would copy from both and the clipboard would end up with
// whichever write landed last.
let pickOwner: symbol | null = null
let dropPick: (() => void) | null = null

export function useCellSelection<T extends Row>(options: SelectionOptions<T>): CellSelection<T> {
  const owner = Symbol('grid')
  const anchor = ref<Cell | null>(null)
  const corner = ref<Cell | null>(null)
  const dragging = ref(false)
  const copied = ref('')

  // A selection reaching more than one record means those records, so it widens
  // to every column of them rather than staying the rectangle that was dragged.
  const bounds = computed<Bounds | null>(() => {
    const a = anchor.value
    const b = corner.value
    if (!a || !b) return null
    const r0 = Math.min(a.row, b.row)
    const r1 = Math.max(a.row, b.row)
    const oneRow = r1 === r0
    return {
      r0,
      r1,
      c0: oneRow ? Math.min(a.col, b.col) : 0,
      c1: oneRow ? Math.max(a.col, b.col) : options.columns().length - 1,
      whole: !oneRow,
    }
  })

  const picked = computed(() => {
    const b = bounds.value
    if (!b) return ''
    const n = (b.whole ? b.r1 - b.r0 : b.c1 - b.c0) + 1
    return n + ' ' + (b.whole ? (n === 1 ? 'record' : 'records') : n === 1 ? 'cell' : 'cells')
  })

  const pickedWhole = computed(() => bounds.value?.whole ?? false)

  const pickedRows = computed<T[]>(() => {
    const b = bounds.value
    if (!b) return []
    const out: T[] = []
    for (let row = b.r0; row <= b.r1; row++) {
      const found = options.rows()[row]
      if (found) out.push(found)
    }
    return out
  })

  function isPicked(row: number, col: number): boolean {
    const b = bounds.value
    return b !== null && row >= b.r0 && row <= b.r1 && col >= b.c0 && col <= b.c1
  }

  function clearSelection(): void {
    anchor.value = null
    corner.value = null
    copied.value = ''
  }

  function claim(): void {
    if (pickOwner === owner) return
    dropPick?.()
    pickOwner = owner
    dropPick = clearSelection
  }

  function selectCell(row: number, col: number): void {
    claim()
    anchor.value = { row, col }
    corner.value = { row, col }
  }

  function onCellDown(row: number, col: number, event: MouseEvent): void {
    // Left button only: a middle click is a paste or a scroll gesture, and a
    // right click belongs to the context menu.
    if (event.button !== 0) return
    // Shift-click extends a pick that is already this grid's, so it must not
    // claim -- claiming would clear the very selection it is extending.
    if (event.shiftKey && anchor.value) {
      corner.value = { row, col }
    } else {
      claim()
      anchor.value = { row, col }
      corner.value = { row, col }
    }
    dragging.value = true
  }

  function onCellOver(row: number, col: number): void {
    if (dragging.value) corner.value = { row, col }
  }

  function endDrag(): void {
    dragging.value = false
  }

  async function copySelection(): Promise<void> {
    const b = bounds.value
    if (!b) return
    const columns = options.columns()
    const lines: string[] = []
    for (let r = b.r0; r <= b.r1; r++) {
      const row = options.rows()[r]
      if (!row) continue
      lines.push(
        columns.slice(b.c0, b.c1 + 1).map((name) => options.text(row[name])).join('\t'),
      )
    }
    try {
      await navigator.clipboard.writeText(lines.join('\n'))
      copied.value = lines.length + (lines.length === 1 ? ' row copied' : ' rows copied')
      window.setTimeout(() => {
        copied.value = ''
      }, 2000)
    } catch {
      copied.value = 'clipboard unavailable'
    }
  }

  function onKeydown(event: KeyboardEvent): void {
    if (options.enabled && !options.enabled()) return
    const target = event.target as HTMLElement | null
    if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'c') {
      if (bounds.value) {
        event.preventDefault()
        copySelection()
      }
    } else if (event.key === 'Escape') {
      clearSelection()
    }
  }

  onMounted(() => {
    window.addEventListener('mouseup', endDrag)
    window.addEventListener('blur', endDrag)
    window.addEventListener('keydown', onKeydown)
  })

  onUnmounted(() => {
    window.removeEventListener('mouseup', endDrag)
    window.removeEventListener('blur', endDrag)
    window.removeEventListener('keydown', onKeydown)
    if (pickOwner === owner) {
      pickOwner = null
      dropPick = null
    }
  })

  return {
    picked,
    pickedWhole,
    pickedRows,
    copied,
    isPicked,
    selectCell,
    onCellDown,
    onCellOver,
    clearSelection,
    copySelection,
  }
}
