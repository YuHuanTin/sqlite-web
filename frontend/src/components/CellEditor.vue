<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, displayValue, inputValue, isBlob } from '../api'
import {
  applyAsciiEdit, asciiLines, bytesToBase64, caretAfterDigits, countDigits,
  formatHex, hexDump, hexLines, parseHex,
} from '../hex'
import { errorText, state } from '../store'
import type { CellValue, ColumnInfo } from '../types'

const props = defineProps<{
  table?: string | null
  column: string
  value: CellValue
  /** null means the row cannot be addressed, which is what makes this read-only. */
  rowKey: string | null
  canWrite: boolean
  info?: ColumnInfo | null
}>()

const emit = defineEmits<{
  close: []
  saved: []
}>()

const blob = computed(() => isBlob(props.value))
const editable = computed(() => props.canWrite && props.rowKey !== null)
const empty = computed(() => props.value === null || props.value === undefined)

function initialText(): string {
  const value = props.value
  return isBlob(value) ? hexLines(value.b64) : displayValue(value)
}

const text = ref(initialText())
const ascii = ref(blob.value ? asciiLines(text.value) : '')
const original = text.value
const changed = computed(() => text.value !== original)

const byteCount = computed(() => (blob.value ? parseHex(text.value).length : 0))
const dump = computed(() => {
  const value = props.value
  return isBlob(value) ? hexDump(value.b64) : ''
})

const saving = ref(false)
const error = ref<string | null>(null)
const copied = ref(false)
const box = ref<HTMLTextAreaElement | null>(null)
const asciiBox = ref<HTMLTextAreaElement | null>(null)

const meta = computed(() => {
  const parts: string[] = []
  const type = props.info?.full_type || props.info?.type
  if (type) parts.push(type)
  if (props.info?.primary_key) parts.push('primary key')
  if (props.info && !props.info.null) parts.push('not null')
  if (empty.value) parts.push('NULL')
  else if (blob.value) parts.push(`${byteCount.value} bytes · hex`)
  else parts.push(`${text.value.length} chars`)
  return parts.join(' · ')
})

onMounted(() => box.value?.focus())

// Both panes write to the same bytes, so each edit has to leave the other
// showing the result. Writing to the element as well as to the ref matters: a
// keystroke that survives no formatting -- a stray character typed into the hex
// -- would otherwise leave the box showing text the value does not have.
function draw(element: HTMLTextAreaElement | null, value: string): void {
  if (element && element.value !== value) element.value = value
}

function onHexInput(event: Event): void {
  const element = event.target as HTMLTextAreaElement
  const before = countDigits(element.value.slice(0, element.selectionStart ?? 0))
  text.value = formatHex(element.value)
  ascii.value = asciiLines(text.value)
  draw(element, text.value)
  draw(asciiBox.value, ascii.value)
  const caret = caretAfterDigits(text.value, before)
  element.setSelectionRange(caret, caret)
}

function onAsciiInput(event: Event): void {
  const element = event.target as HTMLTextAreaElement
  const caret = element.selectionStart ?? 0
  text.value = applyAsciiEdit(text.value, element.value)
  ascii.value = asciiLines(text.value)
  draw(element, ascii.value)
  draw(box.value, text.value)
  const at = Math.min(caret, ascii.value.length)
  element.setSelectionRange(at, at)
}

/** A newline would break the line-for-line correspondence between the panes. */
function noNewlines(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.metaKey && !event.ctrlKey) event.preventDefault()
}

function syncScroll(from: HTMLElement | null, to: HTMLElement | null): void {
  if (from && to) to.scrollTop = from.scrollTop
}

async function copy(): Promise<void> {
  error.value = null
  try {
    await navigator.clipboard.writeText(text.value)
    copied.value = true
    window.setTimeout(() => {
      copied.value = false
    }, 1500)
  } catch {
    error.value = 'The clipboard is not available here. Select the text and press Ctrl+C.'
  }
}

function payload(): Exclude<CellValue, undefined> {
  if (!blob.value) return inputValue(text.value, false)
  if (text.value.trim() === '') return null
  return { b64: bytesToBase64(parseHex(text.value)) }
}

async function save(): Promise<void> {
  if (!props.rowKey || !props.table) return
  saving.value = true
  error.value = null
  try {
    await api.update(state.dataset, props.table, props.rowKey, { [props.column]: payload() })
    emit('saved')
  } catch (err) {
    error.value = errorText(err)
  } finally {
    saving.value = false
  }
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') emit('close')
  else if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) save()
}
</script>

<template>
  <div class="backdrop" @click.self="emit('close')" @keydown="onKeydown">
    <div class="modal" :class="{ wide: blob }">
      <div class="modal-head">
        <span>{{ editable ? 'Edit cell' : 'Cell' }}</span>
        <span v-if="table" class="badge">{{ table }}</span>
        <span class="badge">{{ column }}</span>
        <div class="spacer"></div>
        <button class="btn sm" @click="emit('close')">close</button>
      </div>

      <div class="modal-body">
        <div v-if="error" class="error">{{ error }}</div>

        <div class="field">
          <label>value<span class="meta" style="margin-left: 8px">{{ meta }}</span></label>

          <template v-if="blob && editable">
            <div class="hex-edit">
              <textarea
                ref="box"
                class="input hex-text"
                :value="text"
                spellcheck="false"
                autocomplete="off"
                wrap="off"
                rows="12"
                @input="onHexInput"
                @keydown="noNewlines"
                @scroll="syncScroll(box, asciiBox)"
              ></textarea>
              <textarea
                ref="asciiBox"
                class="input hex-ascii"
                :value="ascii"
                spellcheck="false"
                autocomplete="off"
                wrap="off"
                rows="12"
                @input="onAsciiInput"
                @keydown="noNewlines"
                @scroll="syncScroll(asciiBox, box)"
              ></textarea>
            </div>
            <div class="meta" style="margin-top: 4px">
              either side edits the same bytes &middot; saved as base64, the form the API reads it back in
            </div>
          </template>

          <pre v-else-if="blob" class="cell-view">{{ dump }}</pre>

          <textarea
            v-else-if="editable"
            ref="box"
            v-model="text"
            class="input cell-text"
            spellcheck="false"
            rows="10"
          ></textarea>

          <pre v-else class="cell-view">{{ empty ? 'NULL' : text }}</pre>
        </div>
      </div>

      <div class="modal-foot">
        <span style="color: var(--muted); font-size: 12px; margin-right: auto">
          <template v-if="editable">empty = NULL</template>
          <template v-else-if="!canWrite">read-only</template>
          <template v-else>no key on this row, so it cannot be updated</template>
        </span>
        <button class="btn" @click="copy">{{ copied ? 'copied' : 'Copy' }}</button>
        <button class="btn" @click="emit('close')">Cancel</button>
        <button
          v-if="editable"
          class="btn primary"
          :disabled="saving || !changed"
          @click="save"
        >
          {{ saving ? 'Saving...' : 'Save' }}
        </button>
      </div>
    </div>
  </div>
</template>
