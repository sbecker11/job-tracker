import type { WaitItem } from '../types'
import { ChannelBadge } from './ChannelBadge'

export function WaitQueue({ items }: { items: WaitItem[] }) {
  if (!items.length) {
    return <p className="empty-hint">Nothing waiting on a recruiter reply.</p>
  }

  return (
    <div className="card-list">
      {items.map((item) => (
        <article key={item.normalizedKey} className="action-card quiet">
          <header className="action-card-head">
            <div>
              <strong className="who">
                {item.company} — {item.title}
              </strong>
              <div className="meta">
                Waiting {item.waitingDays}d · status {item.status || '—'}
              </div>
            </div>
            <div className="badges">
              <ChannelBadge channel={item.channel} />
              <span className="meta">{item.contactAttempts}× contact</span>
            </div>
          </header>
          {item.actionHint && <p className="hint-line">{item.actionHint}</p>}
        </article>
      ))}
    </div>
  )
}
