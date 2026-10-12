<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { confirmAction } from './dialog'
import {
  activateTab, applyLocationHash, bootstrap, clearMessages, closeTab, errorText,
  loadTables, openSql, openTable, restoreFromHash, selectDataset, setView, state, VIEWS,
} from './store'
import type { TableInfo } from './types'
import type { SqlTab, Tab, TableTab, View } from './store'
import ConfirmDialog from './components/ConfirmDialog.vue'
import RowsView from './components/RowsView.vue'
import SchemaView from './components/SchemaView.vue'
import SqlConsole from './components/SqlConsole.vue'

const filter = ref('')

const FOLDED_KEY = 'sqlite-web.sidebar-folded'

function wasFolded(): boolean {
  try {
    return window.localStorage.getItem(FOLDED_KEY) === '1'
  } catch {
    return false
  }
}

const folded = ref(wasFolded())

watch(folded, () => {
  try {
    window.localStorage.setItem(FOLDED_KEY, folded.value ? '1' : '0')
  } catch {
    // The sidebar still folds; it just will not be folded next time.
  }
})

const visibleTables = computed<TableInfo[]>(() => {
  const needle = filter.value.trim().toLowerCase()
  if (!needle) return state.tables
  return state.tables.filter((t) => t.name.toLowerCase().includes(needle))
})

const tables = computed(() => visibleTables.value.filter((t) => !t.is_view))
const views = computed(() => visibleTables.value.filter((t) => t.is_view))

const tab = computed<Tab | null>(
  () => state.tabs.find((t) => t.id === state.activeId) ?? null,
)
const tableTab = computed(() => (tab.value?.kind === 'table' ? tab.value : null))
const fills = computed(() => tableTab.value?.view === 'rows')

const sqlTabs = computed(() => state.tabs.filter((t): t is SqlTab => t.kind === 'sql'))
const tableTabs = computed(() => state.tabs.filter((t): t is TableTab => t.kind === 'table'))

function tabLabel(t: Tab): string {
  return t.kind === 'sql' ? t.label : t.table
}

function isViewTab(t: Tab): boolean {
  return t.kind === 'table' && Boolean(state.tables.find((x) => x.name === t.table)?.is_view)
}

function formatCount(count: number | null): string {
  return count === null ? '' : count.toLocaleString()
}

const strip = ref<HTMLElement | null>(null)
const menu = ref<{ x: number; y: number; tab: Tab } | null>(null)

function closeMenu(): void {
  menu.value = null
}

async function requestClose(...tabs: Tab[]): Promise<void> {
  const risky = tabs.filter((t): t is SqlTab => t.kind === 'sql' && t.dirty)
  if (risky.length) {
    const ok = await confirmAction({
      title: risky.length === 1
        ? `Close "${risky[0].label}"?`
        : `Close ${risky.length} query tabs?`,
      message: 'The SQL in ' + (risky.length === 1 ? 'it' : 'them') +
        ' has not been run or saved anywhere, and closing is the only way to lose it.',
      confirmLabel: 'Close',
      danger: true,
    })
    if (!ok) return
  }
  for (const tab of tabs) closeTab(tab.id)
}

function closeOthers(tab: Tab): void {
  requestClose(...state.tabs.filter((t) => t.id !== tab.id))
}

function openMenu(event: MouseEvent, tab: Tab): void {
  menu.value = {
    x: Math.min(event.clientX, window.innerWidth - 170),
    y: Math.min(event.clientY, window.innerHeight - 80),
    tab,
  }
}

function onWheel(event: WheelEvent): void {
  if (strip.value) strip.value.scrollLeft += event.deltaY || event.deltaX
}

watch(
  () => state.activeId,
  async () => {
    await nextTick()
    strip.value
      ?.querySelector('.tab-item.active')
      ?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  },
)

function onMenuKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') closeMenu()
}

async function refresh(): Promise<void> {
  clearMessages()
  try {
    await loadTables()
  } catch (err) {
    state.error = errorText(err)
  }
}

function onDatasetChange(event: Event): void {
  selectDataset((event.target as HTMLSelectElement).value)
}

function onTableClick(name: string): void {
  openTable(name)
}

function onViewClick(view: View): void {
  setView(view)
}

function onHashChange(): void {
  applyLocationHash()
}

// A full reload, not a link to /login/: the browser has to reach this page
// unauthenticated so the server records where to come back to afterwards.
function requestLogin(): void {
  window.location.reload()
}

onMounted(async () => {
  await bootstrap()
  await restoreFromHash()
  window.addEventListener('hashchange', onHashChange)
  window.addEventListener('click', closeMenu)
  window.addEventListener('keydown', onMenuKey)
})

onUnmounted(() => {
  window.removeEventListener('hashchange', onHashChange)
  window.removeEventListener('click', closeMenu)
  window.removeEventListener('keydown', onMenuKey)
})
</script>

<template>
  <div v-if="state.authRequired" class="layout">
    <main class="main">
      <div class="content">
        <div class="empty">
          <p>This server requires a password.</p>
          <p style="margin-top: 12px">
            <button class="btn primary" @click="requestLogin">Log in</button>
          </p>
        </div>
      </div>
    </main>
  </div>

  <div v-else class="layout">
    <button
      v-if="folded"
      class="sidebar-rail"
      title="Show the table list"
      @click="folded = false"
    >
      &raquo;
    </button>

    <aside v-else class="sidebar">
      <div class="sidebar-head">
        <div class="brand">
          <span>sqlite-web</span>
          <span v-if="state.readonly" class="badge ro">read-only</span>
          <div class="spacer"></div>
          <button class="btn sm" title="Fold the sidebar away" @click="folded = true">
            &laquo;
          </button>
        </div>
        <select
          v-if="state.datasets.length"
          class="select"
          style="width: 100%"
          :value="state.dataset"
          @change="onDatasetChange"
        >
          <option v-for="d in state.datasets" :key="d.key" :value="d.key">
            {{ d.basename }}{{ d.is_readonly ? ' (ro)' : '' }}
          </option>
        </select>
      </div>

      <div class="sidebar-scroll">
        <input v-model="filter" class="input" style="width: 100%" placeholder="filter tables..." />

        <div class="section-label">
          <span>Tables</span>
          <span class="count">{{ tables.length }}</span>
        </div>
        <ul class="table-list">
          <li
            v-for="t in tables"
            :key="t.name"
            :class="{ active: tableTab?.table === t.name }"
            @click="onTableClick(t.name)"
          >
            <span class="name" :title="t.name">{{ t.name }}</span>
            <span v-if="!t.has_usable_pk" class="badge" title="no usable primary key">no pk</span>
            <span class="count">{{ formatCount(t.count) }}</span>
          </li>
          <li v-if="!tables.length" class="empty" style="padding: 6px">none</li>
        </ul>

        <template v-if="views.length">
          <div class="section-label">
            <span>Views</span>
            <span class="count">{{ views.length }}</span>
          </div>
          <ul class="table-list">
            <li
              v-for="t in views"
              :key="t.name"
              :class="{ active: tableTab?.table === t.name }"
              @click="onTableClick(t.name)"
            >
              <span class="name" :title="t.name">{{ t.name }}</span>
              <span class="badge view">view</span>
              <span class="count">{{ formatCount(t.count) }}</span>
            </li>
          </ul>
        </template>
      </div>
    </aside>

    <main class="main">
      <div ref="strip" class="tabstrip" @wheel.prevent="onWheel">
        <button
          v-for="t in state.tabs"
          :key="t.id"
          class="tab-item"
          :class="{ active: t.id === state.activeId, view: isViewTab(t) }"
          :title="tabLabel(t)"
          @click="activateTab(t.id)"
          @contextmenu.prevent="openMenu($event, t)"
        >
          <span class="tab-name">{{ tabLabel(t) }}</span>
          <span class="tab-x" title="Close" @click.stop="requestClose(t)">&times;</span>
        </button>
        <span v-if="!state.tabs.length" class="tab-none">nothing open</span>
      </div>

      <div
        v-if="menu"
        class="menu"
        :style="{ left: menu.x + 'px', top: menu.y + 'px' }"
      >
        <button @click="requestClose(menu.tab)">Close</button>
        <button class="danger" @click="closeOthers(menu.tab)">Close others</button>
      </div>

      <div class="topbar">
        <button class="btn" title="New query tab" @click="openSql()">SQL</button>
        <template v-if="tableTab">
          <div class="tabs">
            <button
              v-for="v in VIEWS"
              :key="v"
              class="tab"
              :class="{ active: tableTab.view === v }"
              @click="onViewClick(v)"
            >
              {{ v === 'rows' ? 'Data' : 'Structure' }}
            </button>
          </div>
          <strong style="margin-left: 4px">{{ tableTab.table }}</strong>
          <span v-if="state.readonly" class="badge ro">read-only</span>
        </template>
        <div class="spacer"></div>
        <button class="btn" :disabled="state.loading" @click="refresh">Refresh</button>
      </div>

      <div class="content" :class="{ fill: fills }">
        <div v-if="state.error" class="error">{{ state.error }}</div>
        <div v-else-if="state.notice" class="notice">{{ state.notice }}</div>

        <div v-if="!state.booted" class="empty">Loading...</div>
        <div v-else-if="!state.datasets.length" class="empty">No databases are loaded.</div>
        <div v-else-if="!state.tabs.length" class="empty">
          Nothing is open. Pick a table on the left, or start a query with SQL.
        </div>

        <template v-else>
          <!-- v-show, not v-if: a console keeps its query and its results while
               another tab is in front. -->
          <SqlConsole
            v-for="t in sqlTabs"
            v-show="t.id === state.activeId"
            :key="t.id"
            :tab-id="t.id"
            :initial="t.initial"
          />

          <div v-for="t in tableTabs" v-show="t.id === state.activeId" :key="t.id" class="pane">
            <RowsView v-if="t.view === 'rows'" :table="t.table" />
            <SchemaView v-else :table="t.table" />
          </div>
        </template>
      </div>
    </main>
  </div>

  <ConfirmDialog />
</template>
