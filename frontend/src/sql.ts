import { hexDigits } from './hex'
import type { CellValue } from './types'

function quoteName(name: string): string {
  return '"' + name.replace(/"/g, '""') + '"'
}

export function literal(value: Exclude<CellValue, undefined>): string {
  if (value === null) return 'NULL'
  if (typeof value === 'number') return String(value)
  if (typeof value === 'boolean') return value ? '1' : '0'
  if (typeof value === 'object') return "X'" + hexDigits(value.b64) + "'"
  return "'" + value.replace(/'/g, "''") + "'"
}

export function selectUsing(table: string, column: string, value: CellValue): string {
  // A missing value has to be IS NULL: `= NULL` is never true.
  const where =
    value === null || value === undefined
      ? `${quoteName(column)} IS NULL`
      : `${quoteName(column)} = ${literal(value)}`
  return `SELECT *\nFROM ${quoteName(table)}\nWHERE ${where};`
}
