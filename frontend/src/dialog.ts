import { reactive } from 'vue'

export interface ConfirmOptions {
  title: string
  message?: string
  detail?: string
  confirmLabel?: string
  danger?: boolean
}

interface DialogState {
  open: boolean
  title: string
  message: string
  detail: string
  confirmLabel: string
  danger: boolean
  answer: ((ok: boolean) => void) | null
}

export const dialog = reactive<DialogState>({
  open: false,
  title: '',
  message: '',
  detail: '',
  confirmLabel: 'OK',
  danger: false,
  answer: null,
})

function settle(ok: boolean): void {
  const answer = dialog.answer
  dialog.answer = null
  dialog.open = false
  if (answer) answer(ok)
}

export function confirmAction(options: ConfirmOptions): Promise<boolean> {
  // One at a time: a second question would leave the first one's promise waiting
  // on a dialog that is no longer on screen.
  settle(false)
  return new Promise<boolean>((resolve) => {
    dialog.title = options.title
    dialog.message = options.message ?? ''
    dialog.detail = options.detail ?? ''
    dialog.confirmLabel = options.confirmLabel ?? 'OK'
    dialog.danger = options.danger ?? false
    dialog.answer = resolve
    dialog.open = true
  })
}

export function confirmDialog(): void {
  settle(true)
}

export function cancelDialog(): void {
  settle(false)
}
