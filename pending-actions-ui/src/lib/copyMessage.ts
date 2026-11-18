/** Copy the reply the way the context-menu Copy does.

`navigator.clipboard.writeText` leaves a previous rich-text copy on the
pasteboard, so a later paste still inserts that older text. Selecting the
reply and running the browser copy command replaces the pasteboard.
*/
export function copyMessageText(text: string, anchor?: HTMLElement | null): boolean {
  const existing = findReplyTextarea(anchor, text)
  if (existing) return selectAndCopy(existing)
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('aria-hidden', 'true')
  area.style.position = 'fixed'
  area.style.top = '0'
  area.style.left = '0'
  document.body.appendChild(area)
  try {
    return selectAndCopy(area)
  } finally {
    window.setTimeout(() => area.remove(), 0)
  }
}

export function sameMessage(copied: string, text: string): boolean {
  const a = copied.replace(/\r\n/g, '\n').trim()
  const b = text.replace(/\r\n/g, '\n').trim()
  return a.length > 0 && a === b
}

/** Text a copy event is about to put on the clipboard, from the focused field or the selection. */
export function textFromCopyTarget(target: EventTarget | null): string {
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement) {
    const start = target.selectionStart ?? 0
    const end = target.selectionEnd ?? 0
    if (end > start) return target.value.slice(start, end)
    return ''
  }
  return document.getSelection()?.toString() ?? ''
}

function findReplyTextarea(anchor: HTMLElement | null | undefined, text: string): HTMLTextAreaElement | null {
  const root = anchor?.closest('.lead-panel, .priority-detail, .followup-preview, .priority-row') ?? document
  for (const area of root.querySelectorAll('textarea')) {
    if (area.value === text) return area
  }
  return null
}

function selectAndCopy(area: HTMLTextAreaElement): boolean {
  area.focus()
  const end = area.value.length
  area.setSelectionRange(0, end)
  try {
    return document.execCommand('copy')
  } catch {
    return false
  }
}
