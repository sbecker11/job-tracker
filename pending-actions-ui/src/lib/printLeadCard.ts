function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (ch) =>
    ch === '&' ? '&amp;' : ch === '<' ? '&lt;' : ch === '>' ? '&gt;' : ch === '"' ? '&quot;' : '&#39;',
  )
}

function leadCardTitle(card: ParentNode): string {
  const text = card.querySelector('.priority-lead')?.textContent?.replace(/\s+/g, ' ').trim()
  return text || 'Lead'
}

/** HTML for the near-black preview window. Chrome's print dialog paints a
 * mid-grey band around the page that a page cannot recolor, so Print opens
 * this window instead of going straight to that dialog. */
export function leadCardPreviewHtml(card: HTMLElement, pageCss: string): string {
  const clone = card.cloneNode(true) as HTMLElement
  clone.querySelector('.card-print')?.remove()
  clone.style.height = 'auto'
  clone.style.overflow = 'visible'
  const liveFields = card.querySelectorAll('textarea')
  clone.querySelectorAll('textarea').forEach((area, i) => {
    const pre = clone.ownerDocument.createElement('pre')
    pre.className = 'draft'
    pre.textContent = liveFields[i]?.value ?? ''
    area.replaceWith(pre)
  })
  const title = leadCardTitle(clone)
  return `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>${escapeHtml(title)}</title>
${pageCss}
<style>
  @page { margin: 0.5in; }
  html, body {
    margin: 0;
    min-height: 100%;
    background: #050608 !important;
    color: #e6e8ec;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }
  .preview-toolbar {
    display: flex;
    justify-content: flex-end;
    padding: 12px 16px 0;
    background: #050608;
  }
  .preview-toolbar button {
    font: inherit;
    color: #e6e8ec;
    background: #1a1e27;
    border: 1px solid #3a4150;
    border-radius: 8px;
    padding: 6px 14px;
    cursor: pointer;
  }
  .preview-page {
    padding: 16px;
    background: #050608;
  }
  .priority-row, .priority-row * {
    color: #e6e8ec !important;
    background: transparent !important;
    box-shadow: none !important;
    text-shadow: none !important;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }
  .priority-row {
    height: auto !important;
    max-height: none !important;
    overflow: visible !important;
    background: #14171d !important;
    border-color: #6a7280 !important;
  }
  .stage-chip, .pkg-chip { border-color: #8b93a1 !important; }
  .draft, textarea, pre { white-space: pre-wrap; }
  @media print {
    .preview-toolbar { display: none !important; }
    .preview-page { padding: 0; background: #050608 !important; }
  }
</style>
</head>
<body>
  <div class="preview-toolbar"><button type="button" onclick="window.print()">Print</button></div>
  <div class="preview-page">${clone.outerHTML}</div>
</body>
</html>`
}

function pageStylesheetHtml(): string {
  return [...document.querySelectorAll('style, link[rel="stylesheet"]')].map((node) => node.outerHTML).join('\n')
}

/** Open the preview. Returns null when the browser blocks the window. */
export function printLeadCard(card: HTMLElement): Window | null {
  const frame = window.open('', '_blank', 'width=920,height=1000')
  if (!frame) return null
  frame.document.open()
  frame.document.write(leadCardPreviewHtml(card, pageStylesheetHtml()))
  frame.document.close()
  frame.focus()
  return frame
}
