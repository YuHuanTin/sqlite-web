<script setup lang="ts">
import { computed, ref } from 'vue'
import { api, displayValue, inputValue, isBlob } from '../api'
import { errorText, state } from '../store'
import type { ColumnInfo, Row, RowPayload, SchemaPayload } from '../types'

// Only fields whose text changed are sent (edit), and only those filled in
// (insert). Leaving a field out is what stops an untouched BLOB, or a column
// with a DEFAULT, from being rewritten by a form that cannot represent it.
const props = withDefaults(
  defineProps<{
    table: string
    schema: SchemaPayload
    mode: 'insert' | 'edit'
    row?: Row | null
    rowKey?: string | null
  }>(),
  { row: null, rowKey: null },
)

const emit = defineEmits<{
  close: []
  saved: []
}>()

interface Field {
  name: string
  column: string
  type: string
  nullable: boolean
  pk: boolean
  auto: boolean
  blob: boolean
}

const isEdit = computed(() => props.mode === 'edit')
const saving = ref(false)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)

const text = ref<Record<string, string>>({})
const original = ref<Record<string, string>>({})

for (const col of props.schema.columns) {
  const name: string = col.field || col.column
  const value = isEdit.value && props.row ? props.row[name] : null
  original.value[name] = displayValue(value)
  text.value[name] = isEdit.value ? displayValue(value) : ''
}

const fields = computed<Field[]>(() =>
  props.schema.columns.map((col: ColumnInfo) => {
    const name = col.field || col.column
    return {
      name,
      column: col.column,
      type: col.full_type || col.type,
      nullable: col.null,
      pk: col.primary_key,
      auto: col.autoincrement,
      blob: isEdit.value && props.row ? isBlob(props.row[name]) : false,
    }
  }),
)

function changedPayload(): RowPayload {
  const payload: RowPayload = {}
  for (const field of fields.value) {
    const before = original.value[field.name]
    const now = text.value[field.name]
    if (now === before) continue
    payload[field.name] = inputValue(now, field.blob)
  }
  return payload
}

function insertPayload(): RowPayload {
  const payload: RowPayload = {}
  for (const field of fields.value) {
    const now = text.value[field.name]
    if (now === '') continue // let the database default apply
    payload[field.name] = inputValue(now, field.blob)
  }
  return payload
}

async function save(): Promise<void> {
  error.value = null
  notice.value = null
  const payload = isEdit.value ? changedPayload() : insertPayload()

  if (isEdit.value && !Object.keys(payload).length) {
    notice.value = 'Nothing changed.'
    return
  }
  if (!isEdit.value && !Object.keys(payload).length) {
    error.value = 'Fill in at least one column.'
    return
  }

  saving.value = true
  try {
    if (isEdit.value && props.rowKey) {
      await api.update(state.dataset, props.table, props.rowKey, payload)
    } else {
      await api.insert(state.dataset, props.table, payload)
    }
    emit('saved')
  } catch (err) {
    error.value = errorText(err)
  } finally {
    saving.value = false
  }
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') emit('close')
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) save()
}
</script>

<template>
  <div class="backdrop" @click.self="emit('close')" @keydown="onKeydown">
    <div class="modal">
      <div class="modal-head">
        <span>{{ isEdit ? 'Edit row' : 'Insert row' }}</span>
        <span class="badge">{{ table }}</span>
        <div class="spacer"></div>
        <button class="btn sm" @click="emit('close')">close</button>
      </div>

      <div class="modal-body">
        <div v-if="error" class="error">{{ error }}</div>
        <div v-else-if="notice" class="notice">{{ notice }}</div>

        <div v-for="field in fields" :key="field.name" class="field">
          <label :for="'f-' + field.name">
            {{ field.name }}
            <span v-if="field.pk" class="badge">pk</span>
            <span v-if="field.blob" class="badge">blob</span>
            <span v-if="!field.nullable" class="badge ro">not null</span>
          </label>
          <input
            :id="'f-' + field.name"
            v-model="text[field.name]"
            class="input"
            :placeholder="isEdit ? '' : (field.auto ? 'auto' : field.nullable ? 'null' : '')"
          />
          <div class="meta">
            {{ field.type }}
            <template v-if="field.name !== field.column"> &middot; column "{{ field.column }}"</template>
            <template v-if="field.blob"> &middot; base64</template>
          </div>
        </div>
      </div>

      <div class="modal-foot">
        <span style="color: var(--muted); font-size: 12px; margin-right: auto">
          empty = NULL{{ isEdit ? '; unchanged fields are not sent' : '' }}
        </span>
        <button class="btn" @click="emit('close')">Cancel</button>
        <button class="btn primary" :disabled="saving" @click="save">
          {{ saving ? 'Saving...' : 'Save' }}
        </button>
      </div>
    </div>
  </div>
</template>
