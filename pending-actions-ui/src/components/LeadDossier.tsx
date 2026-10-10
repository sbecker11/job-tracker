import type { ContactPriorityItem } from '../priorityQueue'
import { HistoryLink } from './HistoryLink'

const REPLY_VIA: Record<string, string> = {
  linkedin: 'Continue LinkedIn thread',
  email: 'Follow up by email',
  phone: 'Call',
  unknown: 'Channel unclear',
}

interface Props {
  item: ContactPriorityItem
  primaryReplyHref: string | null
  primaryReplyLabel: string
  onDismiss?: () => void
  dismissing?: boolean
  dismissBusy?: boolean
}

export function LeadDossier({
  item,
  primaryReplyHref,
  primaryReplyLabel,
  onDismiss,
  dismissing,
  dismissBusy,
}: Props) {
  const comms = item.lastComms ?? []
  const writeTo = [item.recruiterName, item.recruiterEmail, item.recruiterPhone]
    .filter(Boolean)
    .join(' · ')

  return (
    <div className="lead-dossier">
      <dl className="dossier-facts">
        <div>
          <dt>Started as</dt>
          <dd>{item.initiation || 'Unknown'}</dd>
        </div>
        <div>
          <dt>Write to</dt>
          <dd>{writeTo || 'No contact on file'}</dd>
        </div>
        <div>
          <dt>Reply via</dt>
          <dd>{REPLY_VIA[item.replyVia || 'unknown']}</dd>
        </div>
        <div>
          <dt>Package</dt>
          <dd>
            {item.packageReady === true
              ? 'Ready'
              : item.packageReady === false
                ? 'Not on disk'
                : 'Unknown'}
          </dd>
        </div>
        <div className="dossier-span">
          <dt>Draft vs history</dt>
          <dd className={item.draftFitOk === false ? 'dossier-warn' : undefined}>
            {item.draftFitNote || 'No draft check.'}
          </dd>
        </div>
        {item.missedMeetingNote ? (
          <div className="dossier-span">
            <dt>Meeting</dt>
            <dd className="dossier-warn">{item.missedMeetingNote}</dd>
          </div>
        ) : null}
      </dl>
      {(primaryReplyHref || onDismiss) && (
        <p className="dossier-reply">
          {primaryReplyHref && (
            <a className="btn link" href={primaryReplyHref} target="_blank" rel="noreferrer">
              {primaryReplyLabel}
            </a>
          )}
          {onDismiss && (
            <button
              type="button"
              className="btn"
              onClick={onDismiss}
              disabled={dismissBusy}
            >
              {dismissing ? 'Marking done…' : 'Mark done'}
            </button>
          )}
        </p>
      )}
      <div className="dossier-history">
        <h3>Recent history</h3>
        {comms.length === 0 ? (
          <p className="hint-line">No stored messages yet.</p>
        ) : (
          <ol>
            {comms.map((c, i) => (
              <li key={`${c.at}-${i}`}>
                <span className="dossier-dir">{c.direction || '?'}</span>
                {c.at ? <span className="dossier-at">{c.at}</span> : null}
                <span>{c.summary || '(no text)'}</span>
              </li>
            ))}
          </ol>
        )}
        {item.company && item.title ? (
          <HistoryLink company={item.company} title={item.title} label="Full history" />
        ) : null}
      </div>
    </div>
  )
}
