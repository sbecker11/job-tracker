import { useState } from 'react'
import { ChannelBadge } from './ChannelBadge'
import { replyAckKey, type ContactPriorityItem } from '../priorityQueue'

const STAGE_LABEL: Record<string, string> = {
  clarify: 'Clarify',
  send_resume: 'Send résumé',
  wait_schedule: 'Wait / schedule',
}

async function copyText(text: string) {
  await navigator.clipboard.writeText(text)
}

function dismissUrl(item: ContactPriorityItem): string | null {
  if (item.stage !== 'clarify') return null
  const kind = item.normalizedKey ? 'lead' : item.kind === 'unmatched' ? 'unmatched' : 'lead'
  const params = new URLSearchParams()
  params.set('kind', kind)
  if (item.normalizedKey) params.set('key', item.normalizedKey)
  if (item.messageId) params.set('message_id', item.messageId)
  return `dlr://dismiss?${params.toString()}`
}

function revealFolderUrl(folderPath: string | undefined): string | null {
  if (!folderPath) return null
  return `revealfolder://reveal?path=${encodeURIComponent(folderPath)}`
}

/** Export + open this lead's job_conversations ODT (tools/view-communications/). */
function viewCommunicationsUrl(company?: string, title?: string): string | null {
  if (!company?.trim() || !title?.trim()) return null
  return `viewcomms://open?company=${encodeURIComponent(company)}&title=${encodeURIComponent(title)}`
}

function mailtoUrl(item: ContactPriorityItem): string | null {
  if (!item.recruiterEmail) return null
  const subject = encodeURIComponent(
    [item.title, item.company].filter(Boolean).join(' — ') || 'Résumé',
  )
  const body = encodeURIComponent(item.draftReply || '')
  return `mailto:${item.recruiterEmail}?subject=${subject}&body=${body}`
}

function mailtoLabel(item: ContactPriorityItem): string {
  return item.emailIsLinkedInRelay ? 'LinkedIn reply' : 'Recruiter email'
}

function markSentHref(item: ContactPriorityItem): string | null {
  if (item.markSentUrl) return item.markSentUrl
  if (!item.normalizedKey) return null
  const params = new URLSearchParams()
  params.set('key', item.normalizedKey)
  params.set('channel', item.channel === 'linkedin' ? 'linkedin' : 'email')
  return `mps://mark?${params.toString()}`
}

interface Props {
  items: ContactPriorityItem[]
  filterLabel: string
  onReplySent?: (item: ContactPriorityItem) => void
  /** Keys from replyAckKey — locked after a successful scan for that inbound. */
  replySentDone?: Record<string, true>
  replyScanningId?: string
  replyScanBusy?: boolean
}

export function ContactPriorityQueue({
  items,
  filterLabel,
  onReplySent,
  replySentDone = {},
  replyScanningId = '',
  replyScanBusy = false,
}: Props) {
  const [expandedId, setExpandedId] = useState('')
  const [copiedId, setCopiedId] = useState('')

  if (!items.length) {
    return (
      <p className="empty-hint">
        No contact work in {filterLabel.toLowerCase()} — you&apos;re clear on outbound follow-ups.
      </p>
    )
  }

  return (
    <ol className="priority-list">
      {items.map((item, index) => {
        const open = expandedId === item.id
        const companyHref = revealFolderUrl(item.companyFolderPath)
        const titleHref = revealFolderUrl(item.folderPath)
        const companyText = item.company || ''
        const titleText = item.title || ''
        const fallbackName = item.recruiterName || '(unnamed lead)'
        const nextAction =
          item.nextAction ||
          item.actionHint ||
          'YOUR ACTION: See stage chip and use the buttons on the right.'
        const ackKey = replyAckKey(item)
        const replyAcknowledged = Boolean(replySentDone[ackKey])
        const replyScanning = replyScanningId === item.id
        const showDetail =
          open &&
          (item.stage === 'clarify' ||
            item.stage === 'send_resume' ||
            (item.stage === 'wait_schedule' && item.followUpDue))
        return (
          <li
            key={item.id}
            className={`priority-row${open ? ' open' : ''}${item.followUpDue ? ' follow-up-due' : ''}${item.replyDue ? ' reply-due' : ''}`}
          >
            <div className="priority-main">
              <span className="priority-rank" aria-label={`Priority ${index + 1}`}>
                {index + 1}
              </span>
              <div className="priority-body">
                <div className="priority-title-row">
                  <strong className="priority-lead">
                    {companyText || titleText ? (
                      <>
                        {companyText ? (
                          companyHref ? (
                            <a
                              className="company-link"
                              href={companyHref}
                              title="Open company folder in Finder"
                            >
                              {companyText}
                            </a>
                          ) : (
                            companyText
                          )
                        ) : null}
                        {companyText && titleText ? ' — ' : ''}
                        {titleText ? (
                          titleHref ? (
                            <a
                              className="title-link"
                              href={titleHref}
                              title="Open this role's folder in Finder"
                            >
                              {titleText}
                            </a>
                          ) : (
                            titleText
                          )
                        ) : null}
                      </>
                    ) : (
                      fallbackName
                    )}
                  </strong>
                  <span className={`stage-chip stage-${item.stage}`}>
                    {STAGE_LABEL[item.stage] || item.stage}
                  </span>
                  <ChannelBadge channel={item.channel} href={item.gmailUrl} />
                  {item.stage === 'send_resume' && (
                    <span className={`pkg-chip${item.packageReady ? ' ready' : ' missing'}`}>
                      {item.packageReady ? 'Package ready' : 'Package missing — generate first'}
                    </span>
                  )}
                  {item.followUpDue && (
                    <span className="pkg-chip missing">
                      Follow-up due ({item.waitingDays}d ≥ {item.followUpThresholdDays ?? 7}d)
                    </span>
                  )}
                  {item.replyDue && (
                    <span className="pkg-chip reply-due">
                      Recruiter waiting on you
                      {item.unansweredDays != null ? ` · ${item.unansweredDays}d` : ''}
                    </span>
                  )}
                  {item.replyDue && onReplySent && (
                    <button
                      type="button"
                      className="btn link regen-btn reply-sent-btn"
                      onClick={() => onReplySent(item)}
                      disabled={replyAcknowledged || replyScanBusy}
                      title={
                        replyAcknowledged
                          ? 'Already marked for this recruiter message — re-enables when a new reply is due'
                          : 'After you BCC yourself on the reply: scan Sent + refresh so this card can move to Wait'
                      }
                      aria-busy={replyScanning}
                    >
                      {replyScanning && (
                        <span className="regen-spinner" aria-hidden="true" />
                      )}
                      {replyScanning
                        ? 'Scanning…'
                        : replyAcknowledged
                          ? 'Reply sent ✓'
                          : 'Reply sent'}
                    </button>
                  )}
                </div>
                <p className="next-action">{nextAction}</p>
                <div className="priority-meta">
                  {item.recruiterName && <span>{item.recruiterName}</span>}
                  {item.recruiterEmail && (
                    <span title={item.recruiterEmail}>
                      {mailtoLabel(item)}
                      {item.emailIsLinkedInRelay ? '' : ` · ${item.recruiterEmail}`}
                    </span>
                  )}
                  <span>
                    {item.contactAttempts}× human contact · {item.ageDays}d
                    {item.waitingDays != null ? ` · waiting ${item.waitingDays}d` : ''}
                  </span>
                </div>
              </div>
              <div className="priority-actions">
                {viewCommunicationsUrl(item.company, item.title) && (
                  <a
                    className="btn link"
                    href={viewCommunicationsUrl(item.company, item.title)!}
                    title="Export and open full communications ODT for this lead"
                  >
                    View communications
                  </a>
                )}
                {item.stage === 'clarify' && item.draftReply && (
                  <button
                    type="button"
                    className="btn"
                    onClick={async () => {
                      await copyText(item.draftReply || '')
                      setCopiedId(item.id)
                      setTimeout(() => setCopiedId(''), 1500)
                    }}
                  >
                    {copiedId === item.id ? 'Copied' : 'Copy reply'}
                  </button>
                )}
                {(item.stage === 'clarify' ||
                  item.stage === 'send_resume' ||
                  (item.stage === 'wait_schedule' && item.followUpDue)) && (
                  <button
                    type="button"
                    className="btn"
                    onClick={() => setExpandedId(open ? '' : item.id)}
                  >
                    {open ? 'Hide steps' : 'Show steps'}
                  </button>
                )}
                {item.stage === 'wait_schedule' && !item.followUpDue && (
                  <span className="btn disabled">No send — waiting</span>
                )}
                {item.stage === 'wait_schedule' && item.followUpDue && item.draftReply && (
                  <button
                    type="button"
                    className="btn"
                    onClick={async () => {
                      await copyText(item.draftReply || '')
                      setCopiedId(item.id)
                      setTimeout(() => setCopiedId(''), 1500)
                    }}
                  >
                    {copiedId === item.id ? 'Copied' : 'Copy follow-up'}
                  </button>
                )}
              </div>
            </div>

            {showDetail && item.stage === 'clarify' && (
              <div className="priority-detail">
                <div className="actions">
                  {item.threadUrl ? (
                    <a className="btn link" href={item.threadUrl} target="_blank" rel="noreferrer">
                      Open thread
                    </a>
                  ) : null}
                  {mailtoUrl(item) && (
                    <a className="btn link" href={mailtoUrl(item)!} title={item.recruiterEmail}>
                      {mailtoLabel(item)}
                    </a>
                  )}
                  {item.gmailUrl ? (
                    <a className="btn link" href={item.gmailUrl} target="_blank" rel="noreferrer">
                      Reply in Gmail
                    </a>
                  ) : !item.threadUrl && !mailtoUrl(item) ? (
                    <span className="btn disabled">Reply in Gmail</span>
                  ) : null}
                  {dismissUrl(item) && (
                    <a className="btn link muted" href={dismissUrl(item)!}>
                      Dismiss / marked replied
                    </a>
                  )}
                </div>
                {item.draftReply ? (
                  <pre className="draft">{item.draftReply}</pre>
                ) : (
                  <p className="empty-hint">No generated draft — reply from the thread/inbox.</p>
                )}
              </div>
            )}

            {showDetail && item.stage === 'send_resume' && (
              <div className="priority-detail send-steps">
                <ol className="step-list">
                  {!item.packageReady && (
                    <li>
                      <strong>Generate package</strong> — résumé + cover are not on disk yet
                      (Decide/apply or run <code>apply-package</code>). Folder currently has review
                      docs only.
                    </li>
                  )}
                  <li>
                    <strong>Open package folder</strong>{' '}
                    {item.folderPath ? (
                      <a className="btn link" href={revealFolderUrl(item.folderPath) || '#'}>
                        Open in Finder
                      </a>
                    ) : (
                      <span className="btn disabled">No folder path</span>
                    )}
                  </li>
                  <li>
                    <strong>Copy message &amp; send</strong> to{' '}
                    {item.recruiterName || 'recruiter'}
                    {item.recruiterEmail ? ` (${item.recruiterEmail})` : ''} — attach résumé + cover
                    letter.
                    <div className="actions">
                      {item.draftReply && (
                        <button
                          type="button"
                          className="btn"
                          onClick={async () => {
                            await copyText(item.draftReply || '')
                            setCopiedId(item.id)
                            setTimeout(() => setCopiedId(''), 1500)
                          }}
                        >
                          {copiedId === item.id ? 'Copied' : 'Copy message'}
                        </button>
                      )}
                      {mailtoUrl(item) && (
                        <a className="btn link" href={mailtoUrl(item)!} title={item.recruiterEmail}>
                          {mailtoLabel(item)}
                        </a>
                      )}
                      {item.threadUrl && (
                        <a
                          className="btn link"
                          href={item.threadUrl}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Open LinkedIn thread
                        </a>
                      )}
                    </div>
                    {item.draftReply && <pre className="draft">{item.draftReply}</pre>}
                  </li>
                  <li>
                    <strong>Mark sent → Wait</strong>
                    <div className="actions">
                      {markSentHref(item) && (
                        <a className="btn link" href={markSentHref(item)!}>
                          Mark sent
                        </a>
                      )}
                    </div>
                    <p className="hint-line">
                      Recruiter email / LinkedIn reply (mail): next Sent-folder scan usually
                      auto-detects and moves this to Wait. LinkedIn web paste: click Mark sent.
                      Then Regenerate.
                    </p>
                  </li>
                </ol>
              </div>
            )}

            {showDetail && item.stage === 'wait_schedule' && item.followUpDue && (
              <div className="priority-detail send-steps">
                <ol className="step-list">
                  <li>
                    <strong>Copy follow-up</strong> and re-initiate with{' '}
                    {item.recruiterName || 'the recruiter'}
                    {item.recruiterEmail ? ` (${item.recruiterEmail})` : ''}.
                    <div className="actions">
                      {item.draftReply && (
                        <button
                          type="button"
                          className="btn"
                          onClick={async () => {
                            await copyText(item.draftReply || '')
                            setCopiedId(item.id)
                            setTimeout(() => setCopiedId(''), 1500)
                          }}
                        >
                          {copiedId === item.id ? 'Copied' : 'Copy follow-up'}
                        </button>
                      )}
                      {mailtoUrl(item) && (
                        <a className="btn link" href={mailtoUrl(item)!} title={item.recruiterEmail}>
                          {mailtoLabel(item)}
                        </a>
                      )}
                      {item.threadUrl && (
                        <a
                          className="btn link"
                          href={item.threadUrl}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Open LinkedIn thread
                        </a>
                      )}
                    </div>
                    {item.draftReply && <pre className="draft">{item.draftReply}</pre>}
                  </li>
                  <li>
                    <strong>Mark sent</strong> to reset the Wait clock
                    <div className="actions">
                      {markSentHref(item) && (
                        <a className="btn link" href={markSentHref(item)!}>
                          Mark sent
                        </a>
                      )}
                    </div>
                    <p className="hint-line">
                      Threshold is {item.followUpThresholdDays ?? 7} days (
                      <code>wait_followup_days</code> in framework.yaml). Then Regenerate.
                    </p>
                  </li>
                </ol>
              </div>
            )}
          </li>
        )
      })}
    </ol>
  )
}
