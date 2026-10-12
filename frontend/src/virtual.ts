import { computed, nextTick, onMounted, onUnmounted, ref, watch, type Ref } from 'vue'

// Every cell holds one line of nowrap text and style.css pins the row height, so
// the window is arithmetic. Change one of those and this constant is wrong.
export const ROW_HEIGHT = 28

// Rows drawn past the viewport, so a fast scroll does not show a blank strip.
const OVERSCAN = 10

export function useVirtualRows<T>(scroller: Ref<HTMLElement | null>, rows: () => T[]) {
  const top = ref(0)
  const height = ref(0)
  const head = ref(0)

  // The sticky header sits above the first row and is part of scrollTop.
  const first = computed(() => Math.max(0, Math.floor((top.value - head.value) / ROW_HEIGHT) - OVERSCAN))
  const last = computed(() =>
    Math.min(rows().length, Math.ceil((top.value - head.value + height.value) / ROW_HEIGHT) + OVERSCAN),
  )
  const visible = computed(() => rows().slice(first.value, last.value))
  const padTop = computed(() => first.value * ROW_HEIGHT)
  const padBottom = computed(() => Math.max(0, (rows().length - last.value) * ROW_HEIGHT))

  function onScroll(): void {
    top.value = scroller.value?.scrollTop ?? 0
  }

  function measure(): void {
    const el = scroller.value
    if (!el) return
    height.value = el.clientHeight
    head.value = el.querySelector('thead')?.clientHeight ?? 0
  }

  let observer: ResizeObserver | null = null

  onMounted(() => {
    measure()
    onScroll()
    if (scroller.value) {
      observer = new ResizeObserver(measure)
      observer.observe(scroller.value)
    }
  })

  onUnmounted(() => observer?.disconnect())

  // Fewer rows leave the scroll position past the new end. The browser clamps it
  // and fires no event of its own, so re-read it here.
  watch(rows, async () => {
    await nextTick()
    measure()
    onScroll()
  })

  return { first, visible, padTop, padBottom, onScroll }
}

// Widths are counted rather than measured: with table-layout: fixed the browser
// lays the grid out once instead of fitting every cell to its content.
export function columnWidths(columns: string[], rows: string[][]): string[] {
  const longest = columns.map((name) => name.length)
  for (const row of rows) {
    for (let at = 0; at < row.length; at++) {
      if (row[at].length > longest[at]) longest[at] = row[at].length
    }
  }
  return longest.map((size) => `${Math.min(Math.max(size, 6), 40)}ch`)
}
