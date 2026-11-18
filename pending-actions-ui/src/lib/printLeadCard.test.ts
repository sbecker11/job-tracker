import { afterEach, describe, expect, it, vi } from 'vitest'
import { leadCardPreviewHtml, printLeadCard } from './printLeadCard'

function cardWith(inner: string): HTMLElement {
  const card = document.createElement('div')
  card.className = 'priority-row'
  card.innerHTML = inner
  return card
}

afterEach(() => {
  document.head.querySelectorAll('style, link[rel="stylesheet"]').forEach((node) => node.remove())
  vi.restoreAllMocks()
})

describe('leadCardPreviewHtml', () => {
  it('uses a near-black window, light text, and a darker card', () => {
    const html = leadCardPreviewHtml(cardWith('<strong class="priority-lead">Pattern</strong>'), '')
    expect(html).toContain('background: #050608 !important')
    expect(html).toContain('color: #e6e8ec')
    expect(html).toContain('background: #14171d !important')
    expect(html).toContain('onclick="window.print()"')
    expect(html).toContain('<title>Pattern</title>')
  })

  it('escapes the title and falls back when the lead name is blank', () => {
    const named = leadCardPreviewHtml(
      cardWith(`<strong class="priority-lead">Tom &amp; "Ann's" &lt;CEO&gt;</strong>`),
      '',
    )
    expect(named).toContain('<title>Tom &amp; &quot;Ann&#39;s&quot; &lt;CEO&gt;</title>')
    expect(leadCardPreviewHtml(cardWith('<strong class="priority-lead">   \n  </strong>'), '')).toContain(
      '<title>Lead</title>',
    )
    expect(leadCardPreviewHtml(cardWith('<p>No name</p>'), '')).toContain('<title>Lead</title>')
  })

  it('prints the full draft and drops the on-card Print button', () => {
    const card = cardWith(
      '<strong class="priority-lead">Pattern   —\n Staff</strong><button class="card-print">Print</button>',
    )
    const draft = document.createElement('textarea')
    draft.value = 'Line one\nLine two'
    const empty = document.createElement('textarea')
    card.append(draft, empty)
    const html = leadCardPreviewHtml(card, '<style>.from-page{}</style>')
    expect(html).toContain('<title>Pattern — Staff</title>')
    expect(html).toContain('<pre class="draft">Line one\nLine two</pre>')
    expect(html).toContain('<pre class="draft"></pre>')
    expect(html).not.toContain('card-print')
    expect(html).toContain('height: auto')
    expect(html).toContain('overflow: visible')
    expect(html).toContain('.from-page{}')
    expect(card.querySelector('.card-print')).not.toBeNull()
    expect(draft.value).toBe('Line one\nLine two')
  })
})

describe('printLeadCard', () => {
  it('writes the preview into a new window and focuses it', () => {
    const style = document.createElement('style')
    style.textContent = '.screen-rule { color: red }'
    document.head.append(style)
    const open = vi.fn()
    const write = vi.fn()
    const close = vi.fn()
    const focus = vi.fn()
    const spy = vi.spyOn(window, 'open').mockReturnValue({ document: { open, write, close }, focus } as unknown as Window)

    const frame = printLeadCard(cardWith('<strong class="priority-lead">Weave</strong>'))

    expect(spy).toHaveBeenCalledWith('', '_blank', 'width=920,height=1000')
    expect(open).toHaveBeenCalledOnce()
    expect(close).toHaveBeenCalledOnce()
    expect(focus).toHaveBeenCalledOnce()
    expect(frame).not.toBeNull()
    const html = String(write.mock.calls[0][0])
    expect(html).toContain('<title>Weave</title>')
    expect(html).toContain('.screen-rule { color: red }')
  })

  it('does nothing when the browser blocks the window', () => {
    vi.spyOn(window, 'open').mockReturnValue(null)
    expect(printLeadCard(cardWith('<strong class="priority-lead">Weave</strong>'))).toBeNull()
  })
})
