import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { copyMessageText, sameMessage, textFromCopyTarget } from './copyMessage'

beforeEach(() => {
  document.execCommand = () => false
})

afterEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('copyMessageText', () => {
  it('selects the reply textarea and uses the browser copy command', () => {
    const card = document.createElement('div')
    card.className = 'lead-panel'
    const area = document.createElement('textarea')
    area.value = 'Hello recruiter'
    const button = document.createElement('button')
    card.append(area, button)
    document.body.append(card)
    const copy = vi.spyOn(document, 'execCommand').mockReturnValue(true)

    expect(copyMessageText('Hello recruiter', button)).toBe(true)
    expect(copy).toHaveBeenCalledWith('copy')
    expect(area.selectionStart).toBe(0)
    expect(area.selectionEnd).toBe(area.value.length)
  })

  it('does not claim success when the copy command fails', () => {
    vi.spyOn(document, 'execCommand').mockReturnValue(false)
    expect(copyMessageText('Hello recruiter')).toBe(false)
  })

  it('treats the full reply as copied and ignores a partial selection', () => {
    expect(sameMessage('Hello recruiter\n', 'Hello recruiter')).toBe(true)
    expect(sameMessage('Hello', 'Hello recruiter')).toBe(false)
    const area = document.createElement('textarea')
    area.value = 'Hello recruiter'
    area.selectionStart = 0
    area.selectionEnd = area.value.length
    expect(sameMessage(textFromCopyTarget(area), 'Hello recruiter')).toBe(true)
    area.selectionEnd = 5
    expect(sameMessage(textFromCopyTarget(area), 'Hello recruiter')).toBe(false)
  })

  it('does not copy a different textarea in the same card', () => {
    const card = document.createElement('div')
    card.className = 'priority-row'
    const other = document.createElement('textarea')
    other.value = 'older clipboard text'
    const reply = document.createElement('textarea')
    reply.value = 'the generated reply'
    card.append(other, reply)
    document.body.append(card)
    vi.spyOn(document, 'execCommand').mockReturnValue(true)

    copyMessageText('the generated reply', reply)
    expect(reply.selectionStart).toBe(0)
    expect(reply.selectionEnd).toBe(reply.value.length)
    expect(other.selectionStart).toBe(other.selectionEnd)
  })
})
