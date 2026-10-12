<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { errorText, state } from '../store'
import type { PrimaryKey, SchemaPayload } from '../types'

const props = defineProps<{ table: string }>()

const schema = ref<SchemaPayload | null>(null)
const error = ref<string | null>(null)
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    schema.value = await api.schema(state.dataset, props.table)
  } catch (err) {
    error.value = errorText(err)
  } finally {
    loading.value = false
  }
}

function pkLabel(pk: PrimaryKey): string {
  if (!pk) return '(none)'
  if (pk.composite) return pk.fields.join(', ')
  return pk.field
}

onMounted(load)
</script>

<template>
  <div>
    <div v-if="error" class="error">{{ error }}</div>
    <div v-else-if="!schema" class="empty">{{ loading ? 'Loading...' : '' }}</div>

    <template v-else>
      <div class="topbar" style="padding: 0 0 10px; background: none; border: none">
        <span class="badge">primary key: {{ pkLabel(schema.primary_key) }}</span>
        <span v-if="schema.is_view" class="badge view">view</span>
        <span v-if="!schema.has_usable_pk" class="badge">no usable pk</span>
        <span v-if="schema.is_readonly" class="badge ro">read-only</span>
      </div>

      <div class="schema-grid">
        <div class="card">
          <h3>Columns</h3>
          <table class="grid">
            <thead>
              <tr>
                <th class="nosort">Name</th>
                <th class="nosort">Type</th>
                <th class="nosort">Null</th>
                <th class="nosort">Default</th>
                <th class="nosort">Key</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="c in schema.columns" :key="c.column">
                <td class="mono" :title="c.column">{{ c.column }}</td>
                <td class="mono">{{ c.full_type || c.type }}</td>
                <td>{{ c.null ? 'yes' : 'no' }}</td>
                <td class="mono">{{ c.default === null ? '' : c.default }}</td>
                <td>
                  <span v-if="c.primary_key" class="badge">pk</span>
                  <span v-if="c.autoincrement" class="badge">auto</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="card">
          <h3>Indexes ({{ schema.indexes.length }})</h3>
          <table v-if="schema.indexes.length" class="grid">
            <tbody>
              <tr v-for="ix in schema.indexes" :key="ix.name">
                <td class="mono">{{ ix.name }}</td>
                <td class="mono">{{ ix.columns.join(', ') }}</td>
                <td><span v-if="ix.unique" class="badge">unique</span></td>
              </tr>
            </tbody>
          </table>
          <div v-else class="empty">none</div>
        </div>

        <div class="card">
          <h3>Foreign keys ({{ schema.foreign_keys.length }})</h3>
          <table v-if="schema.foreign_keys.length" class="grid">
            <tbody>
              <tr v-for="(fk, i) in schema.foreign_keys" :key="i">
                <td class="mono">{{ fk.column }}</td>
                <td class="mono">&rarr; {{ fk.dest_table }}.{{ fk.dest_column }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="empty">none</div>
        </div>

        <div class="card">
          <h3>Triggers ({{ schema.triggers.length }})</h3>
          <template v-if="schema.triggers.length">
            <pre v-for="(t, i) in schema.triggers" :key="i">{{ t }}</pre>
          </template>
          <div v-else class="empty">none</div>
        </div>

        <div class="card" style="grid-column: 1 / -1">
          <h3>CREATE statement</h3>
          <pre>{{ schema.sql }}</pre>
        </div>
      </div>
    </template>
  </div>
</template>
