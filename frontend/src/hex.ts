const PER_LINE = 16

function bytesOf(b64: string): Uint8Array {
  const binary = atob(b64)
  const out = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i)
  return out
}

function byteToHex(byte: number): string {
  return byte.toString(16).padStart(2, '0')
}

function byteToText(byte: number): string {
  return byte >= 0x20 && byte < 0x7f ? String.fromCharCode(byte) : '.'
}

function isHexDigit(char: string): boolean {
  return (char >= '0' && char <= '9') || (char >= 'a' && char <= 'f') || (char >= 'A' && char <= 'F')
}

// Deliberately lenient: this feeds a live preview, so a stray character is
// dropped rather than raising, and a trailing half byte goes with it.
export function parseHex(text: string): Uint8Array {
  const digits: string[] = []
  for (const char of text) {
    if (isHexDigit(char)) digits.push(char)
  }
  const out = new Uint8Array(Math.floor(digits.length / 2))
  for (let i = 0; i < out.length; i++) {
    out[i] = parseInt(digits[i * 2] + digits[i * 2 + 1], 16)
  }
  return out
}

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary)
}

function hexLine(row: Uint8Array): string {
  const parts = Array.from(row, byteToHex)
  const head = parts.slice(0, 8).join(' ')
  const tail = parts.slice(8).join(' ')
  return tail ? `${head}  ${tail}` : head
}

export function formatHex(text: string): string {
  const bytes = parseHex(text)
  const lines: string[] = []
  for (let at = 0; at < bytes.length; at += PER_LINE) {
    lines.push(hexLine(bytes.subarray(at, at + PER_LINE)))
  }
  return lines.join('\n')
}

export function hexLines(b64: string): string {
  return formatHex(Array.from(bytesOf(b64), byteToHex).join(''))
}

export function asciiLines(hexText: string): string {
  if (!hexText) return ''
  return hexText
    .split('\n')
    .map((line) => Array.from(parseHex(line), byteToText).join(''))
    .join('\n')
}

/**
 * Fold an edit made in the text column back into the hex.
 *
 * Two deliberate consequences, both of which look like bugs and are not: a dot
 * means "this byte is not printable", so typing a dot over one is ignored rather
 * than read as 0x2e; and a line that came back shorter was a deletion, which
 * would shift every later character onto the wrong byte, so it is redrawn as it
 * was. Typing past the end of a line appends bytes.
 */
export function applyAsciiEdit(hexText: string, edited: string): string {
  const sourceLines = hexText.split('\n')
  const editedLines = edited.split('\n')
  const out: number[] = []

  for (let line = 0; line < sourceLines.length; line++) {
    const bytes = Array.from(parseHex(sourceLines[line]))
    const typed = editedLines[line] ?? ''
    if (typed.length < bytes.length) {
      out.push(...bytes)
      continue
    }
    for (let at = 0; at < Math.max(bytes.length, typed.length); at++) {
      const char = typed[at]
      if (char === undefined) continue
      const code = char.codePointAt(0) ?? 0
      const printable = code >= 0x20 && code < 0x7f
      if (at < bytes.length) {
        if (char === byteToText(bytes[at]) || !printable) continue
        bytes[at] = code
      } else if (printable) {
        bytes.push(code)
      }
    }
    out.push(...bytes)
  }

  return formatHex(Array.from(out, byteToHex).join(''))
}

export function caretAfterDigits(text: string, digits: number): number {
  let seen = 0
  for (let i = 0; i < text.length; i++) {
    if (isHexDigit(text[i])) {
      seen += 1
      if (seen === digits) return i + 1
    }
  }
  return text.length
}

export function countDigits(text: string): number {
  let seen = 0
  for (const char of text) {
    if (isHexDigit(char)) seen += 1
  }
  return seen
}

/** An empty BLOB renders as nothing: two empty bars look like content. */
export function hexPreview(b64: string, max = 16): string {
  if (!b64) return ''
  // Only the first bytes are drawn, so only the base64 group holding them is
  // decoded: a 4 MB BLOB costs the same as a 16 byte one. The length comes from
  // the string size, which needs no decoding at all.
  const bytes = bytesOf(b64.slice(0, Math.ceil(max / 3) * 4)).subarray(0, max)
  const hex = Array.from(bytes, byteToHex).join(' ')
  const text = Array.from(bytes, byteToText).join('')
  const total = (b64.length / 4) * 3 - (b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0)
  const more = total > max ? '…' : ''
  return `${hex}${more}  |${text}${more}|`
}

export function hexDump(b64: string): string {
  const bytes = bytesOf(b64)
  if (!bytes.length) return '(empty)'
  const lines: string[] = []
  for (let at = 0; at < bytes.length; at += PER_LINE) {
    const row = bytes.subarray(at, at + PER_LINE)
    lines.push(
      at.toString(16).padStart(8, '0') +
        '  ' +
        hexLine(row).padEnd(47) +
        '  |' +
        Array.from(row, byteToText).join('').padEnd(16) +
        '|',
    )
  }
  return lines.join('\n')
}

export function hexDigits(b64: string): string {
  return Array.from(bytesOf(b64), byteToHex).join('')
}
