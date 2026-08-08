import { useState } from 'react'
import type { ClarifyItem } from '../types'
import { ChannelBadge } from './ChannelBadge'

async function copyText(text: string) {
  await navigator.clipboard.writeText(text)
}

function dismissUrl(item: ClarifyItem): string | null {
  const kind = item.normalizedKey ? 'lead' : item.kind === 'unmatched' ? 'unmatched' : 'lead'
  const params = new URLSearchParams()
  params.set('kind', kind)
  if (item.normalizedKey) params.set('key', item.normalizedKey)
  if (item.messageId) params.set('message_id', item.messageId)
  return `dlr://dismiss?${params.toString()}`
}

export function ClarifyQueue({ items }: { items: ClarifyItem[] }) {
  const [copiedId, setCopiedId] = useState('')

  if (!items.length) {
    return <p className="empty-hint">Nothing to clarify — no replyable recruiter threads waiting.</p>
  }

  return (
    <div className="card-list">
      {items.map((item) => {
        const id = item.replyId || item.messageId || item.normalizedKey || item.subject || ''
        const who = item.recruiterName || '(recruiter)'
        const role = [item.company, item.title].filter(Boolean).join(' / ') || item.subject || '(role TBD)'
        return (
          <article key={id} className="action-card">
            <header className="action-card-head">
              <div>
                <strong className="who">{who}</strong>
                <div className="meta">{role}</div>
              </div>
              <div className="badges">
                <ChannelBadge channel={item.channel} />
                <span className="meta">
                  {item.contactAttempts}× contact · {item.ageDays}d
                </span>
              </div>
            </header>
            {item.actionHint && <p className="hint-line">{item.actionHint}</p>}
            <div className="actions">
              <button
                type="button"
                className="btn"
                onClick={async () => {
                  await copyText(item.draftReply || '')
                  setCopiedId(id)
                  setTimeout(() => setCopiedId(''), 1500)
                }}
              >
                {copiedId === id ? 'Copied' : 'Copy reply'}
              </button>
              {item.threadUrl ? (
                <a className="btn link" href={item.threadUrl} target="_blank" rel="noreferrer">
                  Open thread
                </a>
              ) : (
                <span className="btn disabled">Reply in Gmail</span>
              )}
              {dismissUrl(item) && (
                <a className="btn link muted" href={dismissUrl(item)!}>
                  Dismiss / marked replied
                </a>
              )}
            </div>
            <pre className="draft">{item.draftReply}</pre>
          </article>
        )
      })}
    </div>
  )
}
