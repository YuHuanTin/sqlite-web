<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { cancelDialog, confirmDialog, dialog } from '../dialog'

const answer = ref<HTMLButtonElement | null>(null)

watch(
  () => dialog.open,
  async (open) => {
    if (!open) return
    await nextTick()
    answer.value?.focus()
  },
)

// Escape also clears the grid's selection and puts the tab menu away, and a
// question being answered is not the moment to act on what is behind it.
function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.stopPropagation()
    event.preventDefault()
    cancelDialog()
  } else if (event.key === 'Enter') {
    event.stopPropagation()
    event.preventDefault()
    confirmDialog()
  }
}
</script>

<template>
  <div
    v-if="dialog.open"
    class="backdrop"
    @click.self="cancelDialog"
    @keydown="onKeydown"
  >
    <div class="modal" role="alertdialog" aria-modal="true">
      <div class="modal-head">{{ dialog.title }}</div>

      <div class="modal-body">
        <p v-if="dialog.message" style="margin: 0">{{ dialog.message }}</p>
        <pre v-if="dialog.detail" class="cell-view" style="margin-top: 10px">{{ dialog.detail }}</pre>
      </div>

      <div class="modal-foot">
        <button class="btn" @click="cancelDialog">Cancel</button>
        <button
          ref="answer"
          class="btn"
          :class="{ danger: dialog.danger }"
          @click="confirmDialog"
        >
          {{ dialog.confirmLabel }}
        </button>
      </div>
    </div>
  </div>
</template>
