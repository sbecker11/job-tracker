import { useVirtualizer } from '@tanstack/react-virtual'
import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { AllTabsToggle } from './AllTabsToggle'
import { DuplicateBadge } from './DuplicateBadge'
import { ManageLeadStatus } from './ManageLeadStatus'
import { leadAnchorId } from '../lib/links'
import type { GlobalSearchResult } from '../priorityQueue'
import type { DecideLead, WorkflowPayload } from '../types'

/** Rough single-line row estimate — see ArchivedLeadsPanel's identical
 * constant/rationale (2026-08-18 perf pass). "Needs your decision" alone
 * can run to 100+ rows. */
const ESTIMATED_ROW_HEIGHT = 46

function revealFolderUrl(folderPath: string | undefined): string | null {
  if (!folderPath) return null
  return `revealfolder://reveal?path=${encodeURIComponent(folderPath)}`
}

/** Export + open this lead's job_conversations ODT (tools/view-communications/). */
function viewCommunicationsUrl(company?: string, title?: string): string | null {
  if (!company?.trim() || !title?.trim()) return null
  return `viewcomms://open?company=${encodeURIComponent(company)}&title=${encodeURIComponent(title)}`
}

/** Search box filter — company/title/recruiter/email/phone, same fields as
 * ArchivedLeadsPanel / ContactPriorityQueue / DuplicatesSkippedPanel
 * (2026-08-19). */
function matchesQuery(lead: DecideLead, q: string): boolean {
  if (!q) return true
  return (
    lead.company.toLowerCase().includes(q) ||
    lead.title.toLowerCase().includes(q) ||
    (lead.recruiterName?.toLowerCase().includes(q) ?? false) ||
    (lead.recruiterEmail?.toLowerCase().includes(q) ?? false) ||
    (lead.recruiterPhone?.toLowerCase().includes(q) ?? false)
  )
}

function LeadTable({
  title,
  rows,
  showApply,
  onViewDuplicates,
  highlightKey,
}: {
  title: string
  rows: DecideLead[]
  showApply?: boolean
  onViewDuplicates?: (normalizedKey: string, firstDuplicateKey?: string) => void
  highlightKey?: string | null
}) {
  const scrollElRef = useRef<HTMLDivElement | null>(null)
  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollElRef.current,
    estimateSize: () => ESTIMATED_ROW_HEIGHT,
    overscan: 10,
  })

  useEffect(() => {
    if (!highlightKey) return
    const idx = rows.findIndex((r) => r.normalizedKey === highlightKey)
    if (idx >= 0) rowVirtualizer.scrollToIndex(idx, { align: 'center' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [highlightKey])

  if (!rows.length) return null

  const virtualRows = rowVirtualizer.getVirtualItems()
  const totalSize = rowVirtualizer.getTotalSize()
  const topPad = virtualRows.length ? virtualRows[0].start : 0
  const bottomPad = virtualRows.length ? totalSize - virtualRows[virtualRows.length - 1].end : 0

  return (
    <section className="subtable">
      <h3>
        {title} <span className="pill">{rows.length}</span>
      </h3>
      <div className="table-scroll table-scroll-virtual" ref={scrollElRef}>
        <table>
          <thead>
            <tr>
              <th>Company</th>
              <th>Title</th>
              <th className="num">Match %</th>
              <th className="num">Age</th>
              <th>YOUR ACTION</th>
              {showApply && <th>Apply</th>}
            </tr>
          </thead>
          <tbody>
            {topPad > 0 && (
              <tr aria-hidden="true" style={{ height: topPad }}>
                <td colSpan={showApply ? 6 : 5} />
              </tr>
            )}
            {virtualRows.map((virtualRow) => {
              const r = rows[virtualRow.index]
              return (
                <tr
                  key={r.normalizedKey}
                  id={leadAnchorId(r.normalizedKey)}
                  data-lead-key={r.normalizedKey}
                  data-index={virtualRow.index}
                  ref={rowVirtualizer.measureElement}
                  className={r.normalizedKey === highlightKey ? 'lead-highlight' : undefined}
                >
                  <td>
                    {r.companyFolderPath ? (
                      <a
                        className="company-link"
                        href={revealFolderUrl(r.companyFolderPath) || '#'}
                        title="Open company folder in Finder"
                      >
                        {r.company}
                      </a>
                    ) : (
                      r.company
                    )}
                    {r.resumeRequested && (
                      <div className="resume-ask-flag">Recruiter asked for résumé</div>
                    )}
                  </td>
                  <td>
                    {r.folderPath ? (
                      <a
                        className="title-link"
                        href={revealFolderUrl(r.folderPath) || '#'}
                        title="Open this role's folder in Finder"
                      >
                        {r.title}
                      </a>
                    ) : (
                      r.title
                    )}
                    <DuplicateBadge
                      count={r.duplicateCount}
                      firstDuplicateKey={r.duplicateKeys?.[0]}
                      onView={
                        onViewDuplicates
                          ? () => onViewDuplicates(r.normalizedKey, r.duplicateKeys?.[0])
                          : undefined
                      }
                    />
                  </td>
                  <td className="num">{r.matchPct ?? '—'}</td>
                  <td className="num">{r.ageDays}d</td>
                  <td className="action-cell">
                    <div className="decide-actions">
                      <span>{r.nextAction || r.actionHint || 'Review reviews → pursue or skip'}</span>
                      {viewCommunicationsUrl(r.company, r.title) && (
                        <a
                          className="btn"
                          href={viewCommunicationsUrl(r.company, r.title)!}
                          title="Export and open full communications ODT for this lead"
                        >
                          History
                        </a>
                      )}
                      <ManageLeadStatus normalizedKey={r.normalizedKey} />
                    </div>
                  </td>
                  {showApply && (
                    <td>
                      {r.applyUrl ? (
                        <a href={r.applyUrl} target="_blank" rel="noreferrer">
                          Apply
                        </a>
                      ) : (
                        <span className="muted">No link</span>
                      )}
                    </td>
                  )}
                </tr>
              )
            })}
            {bottomPad > 0 && (
              <tr aria-hidden="true" style={{ height: bottomPad }}>
                <td colSpan={showApply ? 6 : 5} />
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}

// Memoized — see 2026-08-18 perf pass note on ContactPriorityQueue.
export const DecideApplyStage = memo(function DecideApplyStage({
  data,
  onViewDuplicates,
  highlightKey,
  searchIndex = [],
  onJumpToResult,
}: {
  data: WorkflowPayload['stages']['decideApply']
  onViewDuplicates?: (normalizedKey: string, firstDuplicateKey?: string) => void
  highlightKey?: string | null
  /** Cross-tab search — see AllTabsToggle. */
  searchIndex?: GlobalSearchResult[]
  onJumpToResult?: (result: GlobalSearchResult) => void
}) {
  const [query, setQuery] = useState('')

  const total =
    data.readyToApply.length +
    data.needsDecision.length +
    data.needsDecisionForced.length +
    data.awaitingLlmReview.length +
    data.jdUnresolved.length

  // Clears a stale search term when a jump-to-lead arrives for a row the
  // current query would otherwise hide (2026-08-19, same pattern as
  // ArchivedLeadsPanel/ContactPriorityQueue).
  useEffect(() => {
    if (!highlightKey) return
    const all = [
      ...data.readyToApply,
      ...data.needsDecision,
      ...data.needsDecisionForced,
      ...data.awaitingLlmReview,
      ...data.jdUnresolved,
    ]
    if (!all.some((r) => r.normalizedKey === highlightKey)) return
    setQuery('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [highlightKey, data])

  const q = query.trim().toLowerCase()
  const filtered = useMemo(
    () => ({
      readyToApply: data.readyToApply.filter((r) => matchesQuery(r, q)),
      needsDecision: data.needsDecision.filter((r) => matchesQuery(r, q)),
      needsDecisionForced: data.needsDecisionForced.filter((r) => matchesQuery(r, q)),
      awaitingLlmReview: data.awaitingLlmReview.filter((r) => matchesQuery(r, q)),
      jdUnresolved: data.jdUnresolved.filter((r) => matchesQuery(r, q)),
    }),
    [data, q],
  )
  const filteredTotal =
    filtered.readyToApply.length +
    filtered.needsDecision.length +
    filtered.needsDecisionForced.length +
    filtered.awaitingLlmReview.length +
    filtered.jdUnresolved.length

  if (!total) {
    return <p className="empty-hint">No decide/apply work in the funnel right now.</p>
  }

  return (
    <div className="decide-stack">
      <p className="hint-line">
        Decide/apply is where you read the reviews and choose pursue (generate package) or skip.
        Contact priority Send résumé only appears after the package exists on disk.
      </p>
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
      {!filteredTotal ? (
        <p className="empty-hint">No decide/apply leads match &quot;{query}&quot;.</p>
      ) : (
        <>
          <LeadTable
            title="Ready to apply"
            rows={filtered.readyToApply}
            showApply
            onViewDuplicates={onViewDuplicates}
            highlightKey={highlightKey}
          />
          <LeadTable
            title="Needs your decision"
            rows={filtered.needsDecision}
            onViewDuplicates={onViewDuplicates}
            highlightKey={highlightKey}
          />
          <LeadTable
            title="Needs decision (forced package)"
            rows={filtered.needsDecisionForced}
            onViewDuplicates={onViewDuplicates}
            highlightKey={highlightKey}
          />
          <LeadTable
            title="Awaiting full-LLM-review"
            rows={filtered.awaitingLlmReview}
            onViewDuplicates={onViewDuplicates}
            highlightKey={highlightKey}
          />
          <LeadTable
            title="JD unresolved"
            rows={filtered.jdUnresolved}
            onViewDuplicates={onViewDuplicates}
            highlightKey={highlightKey}
          />
        </>
      )}
    </div>
  )
})
