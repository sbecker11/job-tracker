import { useVirtualizer } from '@tanstack/react-virtual'
import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { AllTabsToggle } from './AllTabsToggle'
import { ChannelBadge } from './ChannelBadge'
import { DuplicateBadge } from './DuplicateBadge'
import { ManageLeadStatus } from './ManageLeadStatus'
import { leadAnchorId } from '../lib/links'
import { replyAckKey, type ContactPriorityItem, type GlobalSearchResult } from '../priorityQueue'

/** Rough starting estimate (collapsed row: rank + title/badges row + next-
 * action line + meta line, plus padding) — corrected per-row once rendered
 * via rowVirtualizer.measureElement, so "Show steps" expand/collapse still
 * lays out correctly (2026-08-18 perf pass, same approach as
 * ArchivedLeadsPanel's table virtualization). */
const ESTIMATED_ROW_HEIGHT = 132
/** Baked into each row's measured height as reserved trailing space, since
 * absolutely-positioned virtualized items can't use the previous flex
 * `gap: 8px` (that only applies to in-flow siblings). */
const ROW_GAP = 8

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

/**
 * shawn.becker@spexture.com's Gmail account-switcher slot (the "u/1" in
 * https://mail.google.com/mail/u/1/...) — matches how Shawn already signs into
 * this account in Chrome, so compose opens from the right sender.
 */
const RECRUITER_REPLY_ACCOUNT_SLOT = 1

/**
 * The recruiting-automation's tracked inbox (RECRUITING_GMAIL_USER in
 * reply_continuity.py). BCC'd on every recruiter-email send so the Sent-folder
 * scanner (comms_fast_cycle.py) can see this reply even though it's sent from
 * the Spexture account, not this inbox itself.
 */
const RECRUITER_REPLY_BCC = 'shawnbecker.recruiting@gmail.com'

/** Opens Gmail compose in the browser (Chrome), pre-filled from this lead, instead of mailto:. */
function mailtoUrl(item: ContactPriorityItem): string | null {
  if (!item.recruiterEmail) return null
  const subject = [item.company, item.title].filter(Boolean).join(' ')
  const params = new URLSearchParams({
    view: 'cm',
    fs: '1',
    tf: '1',
    to: item.recruiterEmail,
    bcc: RECRUITER_REPLY_BCC,
    su: `RE: ${subject}`,
    body: item.draftReply || '',
  })
  return `https://mail.google.com/mail/u/${RECRUITER_REPLY_ACCOUNT_SLOT}/?${params.toString()}`
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
  /** Switch to the Duplicates skipped tab, focused on this lead's group. */
  onViewDuplicates?: (normalizedKey: string, firstDuplicateKey?: string) => void
  /** Briefly flash + scroll to the row for this key (set after jumping in
   * from the Duplicates skipped tab's "Duplicate of" link). */
  highlightKey?: string | null
  /** Cross-tab search — see AllTabsToggle. */
  searchIndex?: GlobalSearchResult[]
  onJumpToResult?: (result: GlobalSearchResult) => void
}

// Memoized: this list can run to hundreds of rows, and App re-renders every
// second to keep the "last generated ... ago" clock live — without memo,
// every one of those ticks would re-render and re-diff the entire list even
// though its own props never changed (2026-08-18 perf pass).
export const ContactPriorityQueue = memo(function ContactPriorityQueue({
  items,
  filterLabel,
  onReplySent,
  replySentDone = {},
  replyScanningId = '',
  replyScanBusy = false,
  onViewDuplicates,
  highlightKey,
  searchIndex = [],
  onJumpToResult,
}: Props) {
  const [expandedId, setExpandedId] = useState('')
  const [copiedId, setCopiedId] = useState('')
  const [query, setQuery] = useState('')

  // Clears a stale search term when a jump-to-lead arrives for a row the
  // current query would otherwise hide (2026-08-19, same pattern as
  // ArchivedLeadsPanel's highlight-vs-filter reset below).
  useEffect(() => {
    if (!highlightKey) return
    if (!items.some((item) => item.normalizedKey === highlightKey)) return
    setQuery('')
  }, [highlightKey, items])

  const visibleItems = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return items
    return items.filter(
      (item) =>
        (item.company || '').toLowerCase().includes(q) ||
        (item.title || '').toLowerCase().includes(q) ||
        (item.recruiterName || '').toLowerCase().includes(q) ||
        (item.recruiterEmail || '').toLowerCase().includes(q) ||
        (item.recruiterPhone || '').toLowerCase().includes(q),
    )
  }, [items, query])

  // 2026-08-18 perf pass: "All contact" alone can run to 500+ rows — same
  // virtualization approach as ArchivedLeadsPanel's table (bounded-height,
  // independently-scrolling box; only rows currently in view + overscan are
  // mounted). Rows here vary in height a lot more (collapsed vs. "Show
  // steps" expanded), so this relies more heavily on measureElement's
  // automatic ResizeObserver-driven remeasurement than the archived table
  // does.
  const scrollElRef = useRef<HTMLDivElement>(null)
  const rowVirtualizer = useVirtualizer({
    count: visibleItems.length,
    getScrollElement: () => scrollElRef.current,
    estimateSize: () => ESTIMATED_ROW_HEIGHT,
    overscan: 8,
  })

  // Single effect owning all "where should this list be scrolled to right
  // now" decisions — jump-to-target takes priority, falling back to
  // resetting scroll on a tab/filter switch (so a much shorter list doesn't
  // start scrolled past its own end). These *must* live in one effect, not
  // two separate ones: a "jump to lead" always changes filterLabel (it's
  // only reachable by switching tabs into this component fresh) and
  // highlightKey together in the same commit, so a second, later-declared
  // effect resetting to top on filterLabel would run right after this one's
  // scrollToIndex and silently undo it every time (2026-08-18, found while
  // verifying the "All contact" virtualization — a genuine race, not a
  // testing/animation-timing artifact). Deliberately omits `items` from the
  // dep array — it's read fresh via closure regardless, and including it
  // would also re-run (and reset scroll) on every 60s background refresh
  // even when neither the tab nor the highlight target changed.
  useEffect(() => {
    if (highlightKey) {
      const index = visibleItems.findIndex((item) => item.normalizedKey === highlightKey)
      if (index >= 0) {
        rowVirtualizer.scrollToIndex(index, { align: 'center' })
        return
      }
    }
    scrollElRef.current?.scrollTo({ top: 0 })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [highlightKey, filterLabel, query, rowVirtualizer])

  if (!items.length) {
    return (
      <p className="empty-hint">
        No contact work in {filterLabel.toLowerCase()} — you&apos;re clear on outbound follow-ups.
      </p>
    )
  }

  return (
    <div>
      <div className="archive-controls">
        <input
          className="archive-search"
          type="search"
          placeholder="Search company, title, recruiter, email, or phone…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {onJumpToResult && (
          <AllTabsToggle query={query} searchIndex={searchIndex} onJumpToResult={onJumpToResult} />
        )}
      </div>
      {!visibleItems.length ? (
        <p className="empty-hint">No {filterLabel.toLowerCase()} leads match &quot;{query}&quot;.</p>
      ) : (
      <div className="priority-list-scroll" ref={scrollElRef}>
      <ol className="priority-list" style={{ position: 'relative', height: rowVirtualizer.getTotalSize() }}>
        {rowVirtualizer.getVirtualItems().map((virtualRow) => {
          const item = visibleItems[virtualRow.index]
          const index = virtualRow.index
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
        const highlighted = Boolean(item.normalizedKey) && item.normalizedKey === highlightKey
        return (
          <li
            key={item.id}
            id={item.normalizedKey ? leadAnchorId(item.normalizedKey) : undefined}
            data-lead-key={item.normalizedKey || undefined}
            data-index={virtualRow.index}
            ref={rowVirtualizer.measureElement}
            className="priority-row-slot"
            style={{
              position: 'absolute',
              top: 0,
              left: 0,
              right: 0,
              transform: `translateY(${virtualRow.start}px)`,
              paddingBottom: ROW_GAP,
            }}
          >
          <div
            className={`priority-row${open ? ' open' : ''}${item.followUpDue ? ' follow-up-due' : ''}${item.replyDue ? ' reply-due' : ''}${highlighted ? ' lead-highlight' : ''}`}
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
                  {item.normalizedKey && (
                    <DuplicateBadge
                      count={item.duplicateCount}
                      firstDuplicateKey={item.duplicateKeys?.[0]}
                      onView={
                        onViewDuplicates
                          ? () => onViewDuplicates(item.normalizedKey!, item.duplicateKeys?.[0])
                          : undefined
                      }
                    />
                  )}
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
                    className="btn"
                    href={viewCommunicationsUrl(item.company, item.title)!}
                    title="Export and open full communications ODT for this lead"
                  >
                    History
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
                <ManageLeadStatus normalizedKey={item.normalizedKey} />
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
                    <a
                      className="btn link"
                      href={mailtoUrl(item)!}
                      target="_blank"
                      rel="noreferrer"
                      title={item.recruiterEmail}
                    >
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
                {item.messageBody?.trim() && (
                  <details className="original-message">
                    <summary>Original message from {item.recruiterName || 'recruiter'}</summary>
                    <pre className="draft original-message-body">{item.messageBody}</pre>
                  </details>
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
                        <a
                          className="btn link"
                          href={mailtoUrl(item)!}
                          target="_blank"
                          rel="noreferrer"
                          title={item.recruiterEmail}
                        >
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
                        <a
                          className="btn link"
                          href={mailtoUrl(item)!}
                          target="_blank"
                          rel="noreferrer"
                          title={item.recruiterEmail}
                        >
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
          </div>
          </li>
        )
        })}
      </ol>
      </div>
      )}
    </div>
  )
})
