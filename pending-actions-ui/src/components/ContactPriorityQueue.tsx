import { useVirtualizer } from '@tanstack/react-virtual'
import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { ChannelBadge } from './ChannelBadge'
import { DuplicateBadge } from './DuplicateBadge'
import type { DeclineReason } from '../types'
import { LEAD_STATUS_OPTIONS, ManageLeadForm, type MessageTarget } from './ManageLeadStatus'
import {
  formatDecidedAt,
  gmailComposeUrl,
  helperQuery,
  leadAnchorHref,
  leadAnchorId,
  leadStatusUrl,
  NO_ROUTE_STEP,
  recordReplyUrl,
  sendRoutes,
} from '../lib/links'
import { HistoryLink } from './HistoryLink'
import {
  replyAckKey,
  matchesSearch,
  searchGlobalIndex,
  type ContactPriorityItem,
  type GlobalSearchResult,
} from '../priorityQueue'

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
  decide_apply: 'Decide / apply',
  duplicates_skipped: 'Duplicate',
  archived: 'Closed',
}

const DECIDE_BUCKET_LABEL: Record<string, string> = {
  readyToApply: 'Ready to apply',
  needsDecision: 'Needs decision',
  needsDecisionForced: 'Forced review',
  awaitingLlmReview: 'Awaiting LLM',
  jdUnresolved: 'JD unresolved',
}

function statusLabel(status?: string): string {
  if (!status) return ''
  return LEAD_STATUS_OPTIONS.find((o) => o.value === status)?.label || status
}

async function copyText(text: string) {
  await navigator.clipboard.writeText(text)
}

function dismissUrl(item: ContactPriorityItem): string | null {
  if (item.stage !== 'clarify') return null
  const kind = item.normalizedKey ? 'lead' : item.kind === 'unmatched' ? 'unmatched' : 'lead'
  return `dlr://dismiss?${helperQuery({ kind, key: item.normalizedKey, message_id: item.messageId })}`
}

function revealFolderUrl(folderPath: string | undefined): string | null {
  if (!folderPath) return null
  return `revealfolder://reveal?path=${encodeURIComponent(folderPath)}`
}

/** Opens Gmail compose in the browser (Chrome), pre-filled from this lead, instead of mailto:. */
function mailtoUrl(item: ContactPriorityItem, text = item.draftReply || ''): string | null {
  if (!item.recruiterEmail) return null
  const subject = [item.company, item.title].filter(Boolean).join(' ')
  return gmailComposeUrl(item.recruiterEmail, `RE: ${subject}`, text)
}

function messageTarget(item: ContactPriorityItem): MessageTarget {
  const subject = [item.company, item.title].filter(Boolean).join(' ')
  const realEmail =
    (item.recruiterEmail && !item.emailIsLinkedInRelay ? item.recruiterEmail : undefined) || item.resolvedEmail
  return {
    channel:
      item.channel === 'linkedin' || item.emailIsLinkedInRelay ? 'linkedin' : realEmail ? 'email' : 'other',
    threadUrl: item.threadUrl || item.resolvedThreadUrl,
    composeTo: realEmail,
    via: item.resolvedVia,
    gmailUrl: item.gmailUrl,
    linkedinUrl: item.recruiterLinkedinUrl || item.resolvedLinkedinUrl,
    companyEmail: item.companyEmail,
    companyContactName: item.companyContactName,
    recruiterOwned: item.hasRecruiter,
    sender: {
      messageText: item.messageBody,
      name: item.recruiterName,
      email: realEmail,
      phone: item.recruiterPhone,
      company: item.company,
      title: item.title,
    },
    subject: `RE: ${subject}`,
    hasRecruiter: Boolean(item.recruiterName || item.recruiterEmail || item.threadUrl),
  }
}

/** LinkedIn message whose only "email" is LinkedIn's notification (replies go to LinkedIn's relay). */
function linkedinNotice(item: ContactPriorityItem): boolean {
  const t = messageTarget(item)
  return t.channel === 'linkedin' && !t.composeTo
}

function mailtoLabel(item: ContactPriorityItem): string {
  return item.emailIsLinkedInRelay ? 'LinkedIn reply' : 'Recruiter email'
}

/** First dialable number in a free-text phone field ("551-325-2207 (mobile) / …"). */
function telHref(phone: string | undefined): string | null {
  const first = (phone || '').match(/\+?\d[\d\s().-]{6,}\d/)
  if (!first) return null
  return `tel:${first[0].replace(/[^\d+]/g, '')}`
}

function markSentHref(item: ContactPriorityItem): string | null {
  if (item.markSentUrl) return item.markSentUrl
  if (!item.normalizedKey) return null
  const channel = item.channel === 'linkedin' ? 'linkedin' : 'email'
  return `mps://mark?${helperQuery({ key: item.normalizedKey, channel })}`
}

interface Props {
  items: ContactPriorityItem[]
  filterLabel: string
  onReplySent?: (item: ContactPriorityItem) => void
  /** Unmatched LinkedIn card: Reply sent opened RecordReply — mark it locally. */
  onReplyRecorded?: (item: ContactPriorityItem) => void
  /** Keys from replyAckKey — locked after a successful scan for that inbound. */
  replySentDone?: Record<string, true>
  replyScanningId?: string
  replyScanBusy?: boolean
  /** Switch to the Duplicates filter, focused on this lead's group. */
  onViewDuplicates?: (normalizedKey: string, firstDuplicateKey?: string) => void
  onJumpToSurvivor?: (normalizedKey: string) => void
  /** Briefly flash + scroll to the row for this key (set after jumping in
   * from the Duplicates skipped tab's "Duplicate of" link). */
  highlightKey?: string | null
  /** Cross-tab search — see AllTabsToggle. */
  searchIndex?: GlobalSearchResult[]
  onJumpToResult?: (result: GlobalSearchResult) => void
  /** Group rows under "Reply to recruiter" / "Apply" / … headings (priority sort only). */
  showTierHeadings?: boolean
  /** Canned "no thank you" reasons + closing for the Decline form. */
  declineReasons?: DeclineReason[]
  declineClosing?: string
  /** A Decline / Dismiss / Manage-lead link was clicked on this row; `hide`
   * drops the row until newer page data arrives. */
  onLeadActioned?: (itemId: string, hide: boolean) => void
}

const NO_DECLINE_REASONS: DeclineReason[] = []

function reopenUrl(item: ContactPriorityItem): string | null {
  if (!item.postDecline || !item.normalizedKey) return null
  return leadStatusUrl(item.normalizedKey, 'pursued', 'Reopened after recruiter replied to decline')
}

// Memoized: this list can run to hundreds of rows, and App re-renders every
// second to keep the "last generated ... ago" clock live — without memo,
// every one of those ticks would re-render and re-diff the entire list even
// though its own props never changed (2026-08-18 perf pass).
export const ContactPriorityQueue = memo(function ContactPriorityQueue({
  items,
  filterLabel,
  onReplySent,
  onReplyRecorded,
  replySentDone = {},
  replyScanningId = '',
  replyScanBusy = false,
  onViewDuplicates,
  onJumpToSurvivor,
  highlightKey,
  searchIndex = [],
  onJumpToResult,
  showTierHeadings = false,
  declineReasons = NO_DECLINE_REASONS,
  declineClosing = '',
  onLeadActioned,
}: Props) {
  const [expandedId, setExpandedId] = useState('')
  const [leadPanel, setLeadPanel] = useState<{ id: string; kind: 'manage' } | null>(null)
  const toggleLeadPanel = (id: string, kind: 'manage') =>
    setLeadPanel((cur) => (cur?.id === id && cur.kind === kind ? null : { id, kind }))
  const [copiedId, setCopiedId] = useState('')
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const draftOf = (it: ContactPriorityItem) => drafts[it.id] ?? it.draftReply ?? ''
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
    if (!query.trim()) return items
    return items.filter((item) =>
      matchesSearch(
        [item.company, item.title, item.recruiterName, item.recruiterEmail, item.recruiterPhone],
        query,
      ),
    )
  }, [items, query])

  const outsideMatches = useMemo(() => {
    const q = query.trim()
    if (!q || !onJumpToResult) return []
    const inList = new Set(items.map((i) => i.normalizedKey).filter(Boolean) as string[])
    return searchGlobalIndex(searchIndex, q).filter((r) => !inList.has(r.normalizedKey))
  }, [query, items, searchIndex, onJumpToResult])

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
        {outsideMatches.length > 0 && onJumpToResult && (
          <div className="global-search-results">
            <p className="global-search-empty">
              {outsideMatches.length} match{outsideMatches.length === 1 ? '' : 'es'} hidden by the
              current filters
            </p>
            <ul>
              {outsideMatches.map((r) => (
                <li key={r.normalizedKey}>
                  <button
                    type="button"
                    className="global-search-result"
                    onClick={() => onJumpToResult(r)}
                  >
                    <span className="global-search-result-main">
                      {r.title} @ {r.company}
                    </span>
                    <span className="global-search-result-meta">
                      {STAGE_LABEL[r.tab] || r.tab}
                      {r.status ? ` · ${statusLabel(r.status)}` : ''}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
      {!items.length ? (
        <p className="empty-hint">
          No matching leads in {filterLabel.toLowerCase()} — change Status or a task chip, or
          you&apos;re clear on this slice.
        </p>
      ) : !visibleItems.length ? (
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
        // Clarify rows with a lead record reply from Manage lead; keyless
        // ones (unmatched InMails) keep Copy reply / Show steps / Reply sent.
        const replyInPanel = item.stage === 'clarify' && Boolean(item.normalizedKey && item.draftReply)
        // LinkedIn never emails back what Shawn sends, so unmatched LinkedIn
        // cards record the reply through RecordReply instead of a Sent scan.
        const recordable =
          item.kind === 'unmatched' &&
          Boolean(item.messageId) &&
          (item.channel === 'linkedin' || Boolean(item.emailIsLinkedInRelay))
        const followupInPanel =
          item.stage === 'wait_schedule' &&
          Boolean(item.followUpDue && item.normalizedKey && item.followupDraft) &&
          !item.staleActive &&
          !item.reopened
        const route = replyInPanel || followupInPanel ? sendRoutes(messageTarget(item))[0] : undefined
        const who = item.recruiterName || 'The recruiter'
        const replyDays = item.unansweredDays ?? item.ageDays
        const panelStatus =
          replyInPanel || (recordable && item.replyDue)
          ? `${who} is waiting on you${replyDays != null ? ` (${replyDays}d)` : ''}`
          : `No reply from ${who === 'The recruiter' ? 'the recruiter' : who} in ${item.waitingDays ?? '?'}d — time to follow up`
        const recordSteps =
          recordable && item.replyDue && !replyInPanel
            ? [
                'Click "Copy reply" and edit it if you like.',
                'Open the LinkedIn thread, paste the reply and press Send.',
                'Click "Reply sent" and confirm what you sent — the card moves to Waiting on them.',
              ]
            : null
        const panelSteps =
          replyInPanel || followupInPanel
            ? [
                replyInPanel
                  ? "Review the reply. It's already written for you."
                  : "Review the follow-up. It's already written for you.",
                route ? route.step : NO_ROUTE_STEP,
                ...(route?.sendsAndLogs ? [] : ['Click "Message sent".']),
              ]
            : null
        const sendTo = route
          ? `${route.how === 'text' || route.how === 'email' ? `${who} · ${route.how} ${route.to}` : `${who} · ${route.how}`}${
              item.resolvedVia && route.to === item.resolvedEmail ? ` (found via ${item.resolvedVia})` : ''
            }`
          : `${who} — no email, LinkedIn or phone on file`
        const steps = panelSteps ?? recordSteps
        const stepsTitle = panelSteps ? `Manage lead (${panelSteps.length} steps):` : 'Steps:'
        const nextAction = steps
          ? ''
            : item.nextAction ||
            item.actionHint ||
            'YOUR ACTION: See stage chip and use the buttons on the right.'
        const ackKey = replyAckKey(item)
        const replyAcknowledged = Boolean(replySentDone[ackKey])
        const replyScanning = replyScanningId === item.id
        const showDetail = open && (item.stage === 'clarify' || item.stage === 'send_resume')
        const highlighted = Boolean(item.normalizedKey) && item.normalizedKey === highlightKey
        const canDecline =
          !item.postDecline &&
          Boolean(item.normalizedKey && item.declineGreeting && declineReasons.length)
        const panelKind = leadPanel?.id === item.id ? leadPanel.kind : null
        const tierHeading =
          showTierHeadings &&
          item.todayLabel &&
          (index === 0 || visibleItems[index - 1].todayTier !== item.todayTier)
            ? item.todayLabel
            : ''
        const phoneHref = telHref(item.recruiterPhone)
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
          {tierHeading && (
            <h3 className={`today-tier-heading today-tier-${item.todayTier}`}>{tierHeading}</h3>
          )}
          <div
            className={`priority-row${open ? ' open' : ''}${item.followUpDue && item.todayTier !== 9 ? ' follow-up-due' : ''}${item.replyDue ? ' reply-due' : ''}${item.todayTier === 9 ? ' probably-dead' : ''}${highlighted ? ' lead-highlight' : ''}`}
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
                    {item.decideBucket
                      ? DECIDE_BUCKET_LABEL[item.decideBucket] || STAGE_LABEL[item.stage]
                      : STAGE_LABEL[item.stage] || item.stage}
                  </span>
                  {item.status && (
                    <span className="stage-chip stage-status">{statusLabel(item.status)}</span>
                  )}
                  {item.matchPct != null && (
                    <span className="stage-chip">{item.matchPct}% match</span>
                  )}
                  {item.verdict && (
                    <span className="stage-chip">{item.verdict}</span>
                  )}
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
                  {item.channel && (
                    <ChannelBadge
                      channel={item.channel}
                      href={item.channel === 'linkedin' && item.threadUrl ? item.threadUrl : item.gmailUrl}
                    />
                  )}
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
                  {item.postDecline && (
                    <span className="pkg-chip declined">
                      Declined: {item.declineReasonLabel || 'skipped'}
                    </span>
                  )}
                  {item.postDecline && item.termsChanged && (
                    <span className="pkg-chip terms-changed">Terms may have changed</span>
                  )}
                  {item.resumeRequested && item.stage === 'decide_apply' && (
                    <span className="pkg-chip missing">Recruiter asked for résumé</span>
                  )}
                  {item.duplicateOfKey && onJumpToSurvivor && (
                    <a
                      className="link-button"
                      href={leadAnchorHref(item.duplicateOfKey)}
                      onClick={(e) => {
                        e.preventDefault()
                        onJumpToSurvivor(item.duplicateOfKey!)
                      }}
                    >
                      Duplicate of {item.duplicateOfTitle || 'this lead'}
                      {item.duplicateOfCompany ? ` @ ${item.duplicateOfCompany}` : ''}
                    </a>
                  )}
                  {item.replyDue && recordable && (
                    <a
                      className={`btn link regen-btn reply-sent-btn${replyAcknowledged ? ' muted' : ''}`}
                      href={recordReplyUrl(item.messageId!, item.recruiterName || '', draftOf(item))}
                      onClick={() => onReplyRecorded?.(item)}
                      title="After you send it on LinkedIn: confirm what you sent, and this card moves to Waiting on them"
                    >
                      {replyAcknowledged ? 'Reply sent ✓' : 'Reply sent'}
                    </a>
                  )}
                  {item.replyDue && onReplySent && !replyInPanel && !recordable && (
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
              </div>
              {steps ? (
                <div className="next-action priority-wide">
                  <p className="next-action-status">Current status: {panelStatus}</p>
                  {panelSteps && <p className="next-action-sendto">Send to: {sendTo}</p>}
                  <p className="next-action-steps-title">{stepsTitle}</p>
                  <ol className="next-action-steps">
                    {steps.map((step) => (
                      <li key={step}>{step}</li>
                    ))}
                  </ol>
                </div>
              ) : (
                <p className="next-action priority-wide">{nextAction}</p>
              )}
              <div className="priority-meta priority-wide">
                {item.recruiterName && <span>{item.recruiterName}</span>}
                {item.recruiterPhone &&
                  (phoneHref ? (
                    <a className="phone-link" href={phoneHref} title="Call">
                      {item.recruiterPhone}
                    </a>
                  ) : (
                    <span>{item.recruiterPhone}</span>
                  ))}
                {item.recruiterEmail && (
                  <span title={item.recruiterEmail}>
                    {mailtoLabel(item)}
                    {item.emailIsLinkedInRelay ? '' : ` · ${item.recruiterEmail}`}
                  </span>
                )}
                <span>
                  {item.contactAttempts
                    ? `${item.contactAttempts}× human contact · ${item.ageDays}d`
                    : `${item.ageDays}d`}
                  {item.waitingDays != null ? ` · waiting ${item.waitingDays}d` : ''}
                  {item.decidedAt ? ` · decided ${formatDecidedAt(item.decidedAt)}` : ''}
                  {item.commCount != null ? ` · ${item.commCount} messages` : ''}
                </span>
              </div>
              <div className="priority-actions">
                {item.company && item.title ? (
                  <HistoryLink company={item.company} title={item.title} />
                ) : null}
                {item.stage === 'clarify' && item.draftReply && !replyInPanel && (
                  <button
                    type="button"
                    className="btn"
                    onClick={async () => {
                      await copyText(draftOf(item))
                      setCopiedId(item.id)
                      setTimeout(() => setCopiedId(''), 1500)
                    }}
                  >
                    {copiedId === item.id ? 'Copied' : 'Copy reply'}
                  </button>
                )}
                {((item.stage === 'clarify' && !replyInPanel) || item.stage === 'send_resume') && (
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
                {item.applyUrl && (
                  <a className="btn" href={item.applyUrl} target="_blank" rel="noreferrer">
                    Apply
                  </a>
                )}
                {reopenUrl(item) && (
                  <a
                    className="btn"
                    href={reopenUrl(item)!}
                    title="Move this lead back to Pursued so it re-enters the normal flow"
                  >
                    Reopen
                  </a>
                )}
                {item.normalizedKey && (
                  <button
                    type="button"
                    className="btn link muted"
                    onClick={() => toggleLeadPanel(item.id, 'manage')}
                  >
                    {panelKind === 'manage' ? 'Manage lead ▲' : 'Manage lead ▾'}
                  </button>
                )}
              </div>
            </div>

            {panelKind && item.normalizedKey && (
              <div className="lead-panel">
                {panelKind === 'manage' && (
                  <ManageLeadForm
                    normalizedKey={item.normalizedKey}
                    currentStatus={item.status}
                    initialStatus={item.stage === 'wait_schedule' && item.followUpDue ? 'following_up' : undefined}
                    onApplied={() => onLeadActioned?.(item.id, false)}
                    onCancel={() => setLeadPanel(null)}
                    message={messageTarget(item)}
                    followupDraft={item.followupDraft}
                    reply={
                      replyInPanel
                        ? {
                            draft: item.draftReply || '',
                            original: item.messageBody,
                            from: item.recruiterName,
                            onSent: () => onLeadActioned?.(item.id, true),
                          }
                        : undefined
                    }
                    decline={
                      canDecline && item.declineGreeting
                        ? {
                            greeting: item.declineGreeting,
                            closing: declineClosing,
                            onClosed: () => onLeadActioned?.(item.id, true),
                          }
                        : undefined
                    }
                  />
                )}
              </div>
            )}

            {showDetail && item.stage === 'clarify' && (
              <div className="priority-detail">
                <div className="actions">
                  {item.threadUrl ? (
                    <a
                      className="btn link"
                      href={item.threadUrl}
                      target="_blank"
                      rel="noreferrer"
                      onClick={() => {
                        if (draftOf(item)) void copyText(draftOf(item))
                      }}
                      title="Opens the thread with the reply copied — paste it and press Send"
                    >
                      {/linkedin\.com/i.test(item.threadUrl) ? 'Reply on LinkedIn' : 'Open thread'}
                    </a>
                  ) : null}
                  {mailtoUrl(item, draftOf(item)) && (
                    <a
                      className="btn link"
                      href={mailtoUrl(item, draftOf(item))!}
                      target="_blank"
                      rel="noreferrer"
                      title={item.recruiterEmail}
                    >
                      {mailtoLabel(item)}
                    </a>
                  )}
                  {item.gmailUrl && linkedinNotice(item) ? (
                    !item.threadUrl && (
                      <a className="btn link" href={item.gmailUrl} target="_blank" rel="noreferrer">
                        Open LinkedIn notification
                      </a>
                    )
                  ) : item.gmailUrl ? (
                    <a className="btn link" href={item.gmailUrl} target="_blank" rel="noreferrer">
                      Reply in Gmail
                    </a>
                  ) : !item.threadUrl && !mailtoUrl(item, draftOf(item)) ? (
                    <span className="btn disabled">Reply in Gmail</span>
                  ) : null}
                  {dismissUrl(item) && (
                    <a
                      className="btn link muted"
                      href={dismissUrl(item)!}
                      onClick={() => onLeadActioned?.(item.id, true)}
                    >
                      Dismiss / marked replied
                    </a>
                  )}
                </div>
                {item.draftReply ? (
                  <textarea
                    className="draft followup-edit"
                    rows={Math.max(6, draftOf(item).split('\n').length + 1)}
                    value={draftOf(item)}
                    onChange={(e) => setDrafts((d) => ({ ...d, [item.id]: e.target.value }))}
                    aria-label="Reply message (editable)"
                  />
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
                            await copyText(draftOf(item))
                            setCopiedId(item.id)
                            setTimeout(() => setCopiedId(''), 1500)
                          }}
                        >
                          {copiedId === item.id ? 'Copied' : 'Copy message'}
                        </button>
                      )}
                      {mailtoUrl(item, draftOf(item)) && (
                        <a
                          className="btn link"
                          href={mailtoUrl(item, draftOf(item))!}
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
                    {item.draftReply && (
                      <textarea
                        className="draft followup-edit"
                        rows={Math.max(6, draftOf(item).split('\n').length + 1)}
                        value={draftOf(item)}
                        onChange={(e) => setDrafts((d) => ({ ...d, [item.id]: e.target.value }))}
                        aria-label="Reply message (editable)"
                      />
                    )}
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
