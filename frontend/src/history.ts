const KEY = 'sqlite-web.sql-history'

export const MAX_HISTORY = 100

export function loadHistory(): string[] {
  try {
    const raw = window.localStorage.getItem(KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : []
    if (!Array.isArray(parsed)) return []
    return parsed.filter((entry): entry is string => typeof entry === 'string').slice(0, MAX_HISTORY)
  } catch {
    return []
  }
}

function save(entries: string[]): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(entries))
  } catch {
    // Storage can refuse (private mode, full quota); the list still works for
    // this session.
  }
}

export function remember(sql: string, current: string[]): string[] {
  const text = sql.trim()
  if (!text) return current
  const next = [text, ...current.filter((entry) => entry !== text)].slice(0, MAX_HISTORY)
  save(next)
  return next
}

export function forgetAll(): string[] {
  save([])
  return []
}

export function oneLine(sql: string, max = 60): string {
  const text = sql.replace(/\s+/g, ' ').trim()
  return text.length > max ? text.slice(0, max) + '…' : text
}
