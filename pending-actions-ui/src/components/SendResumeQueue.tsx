import type { SendResumeItem } from '../types'
import { ChannelBadge } from './ChannelBadge'

function revealFolderUrl(folderPath: string): string | null {
  if (!folderPath) return null
  // Absolute path not known in the browser — use relative path hint via helper when available.
  return `revealfolder://reveal?path=${encodeURIComponent(folderPath)}`
}

export function SendResumeQueue({ items }: { items: SendResumeItem[] }) {
  if (!items.length) {
    return <p className="empty-hint">No résumé-send actions waiting.</p>
  }

  const readyCount = items.filter((i) => i.packageReady).length
  const missingCount = items.length - readyCount

  return (
    <div className="card-list">
      {readyCount > 0 && (
        <p className="hint-line">
          <strong>{readyCount}</strong> package{readyCount === 1 ? '' : 's'} ready to send
          {missingCount > 0 ? ` · ${missingCount} still need generation on disk` : ''}
        </p>
      )}
      {items.map((item) => (
        <article key={item.normalizedKey} className="action-card">
          <header className="action-card-head">
            <div>
              <strong className="who">
                {item.company} — {item.title}
              </strong>
              <div className="meta">
                {item.resumeRequested ? 'Recruiter asked for résumé' : 'Package ready for recruiter'}
                {item.packageReady ? ' · docs on disk' : ' · generate package first'}
              </div>
            </div>
            <div className="badges">
              <ChannelBadge channel={item.channel} href={item.gmailUrl} />
              <span className="meta">
                {item.contactAttempts}× · {item.ageDays}d
              </span>
            </div>
          </header>
          {item.actionHint && <p className="hint-line">{item.actionHint}</p>}
          <div className="actions">
            {item.folderPath && (
              <a className="btn link" href={revealFolderUrl(item.folderPath) || '#'}>
                Open package folder
              </a>
            )}
            {item.applyUrl && (
              <a className="btn link" href={item.applyUrl} target="_blank" rel="noreferrer">
                Apply URL
              </a>
            )}
          </div>
        </article>
      ))}
    </div>
  )
}
