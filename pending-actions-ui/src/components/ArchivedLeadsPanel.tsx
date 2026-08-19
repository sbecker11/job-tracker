import { useVirtualizer } from '@tanstack/react-virtual'
import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { DuplicateBadge } from './DuplicateBadge'
import { formatDecidedAt, leadAnchorId, revealFolderUrl, viewCommunicationsUrl } from '../lib/links'
import type { ArchivedLead } from '../types'

/** Rough single-line row height (px) — just a starting estimate; actual
 * height is measured per-row once rendered (rowVirtualizer.measureElement
 * below), so wrapped duplicate-of text etc. still lays out correctly. */
const ESTIMATED_ROW_HEIGHT = 46

const STATUS_LABELS: Record<string, string> = {
  skipped: 'Skipped',
  rejected: 'Rejected',
  deleted: 'Deleted',
  unavailable: 'Unavailable',
  hired: 'Hired',
  // Applied-or-beyond (2026-08-17) — off the active decide/apply funnel
  // the same way skipped/rejected/etc. are, just via the other exit (the
  // posting was actually submitted) rather than a decision against it.
  applied: 'Applied',
  following_up: 'Following up',
  interviewing: 'Interviewing',
  offered: 'Offered',
  accepted: 'Accepted',
  started: 'Started',
}

// Memoized: this table can run to 1000+ rows (see 2026-08-18 perf pass note
// on ContactPriorityQueue) — this one is the biggest offender since it holds
// every archived/decided lead ever recorded.
export const ArchivedLeadsPanel = memo(function ArchivedLeadsPanel({
  leads,
  onViewDuplicates,
  highlightKey,
}: {
  leads: ArchivedLead[]
  onViewDuplicates?: (normalizedKey: string, firstDuplicateKey?: string) => void
  /** Row to flash on arrival — also clears any stale internal status/
   * duplicate filter that would otherwise hide it (2026-08-17, alongside
   * "Go to this lead" learning to land here for survivors that were
   * themselves later fully decided). */
  highlightKey?: string | null
}) {
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [duplicateFilterKey, setDuplicateFilterKey] = useState<string | null>(null)

  useEffect(() => {
    if (!highlightKey) return
    if (!leads.some((l) => l.normalizedKey === highlightKey)) return
    setStatusFilter('all')
    setDuplicateFilterKey(null)
    setQuery('')
  }, [highlightKey, leads])

  const statusCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const l of leads) counts[l.status] = (counts[l.status] || 0) + 1
    return counts
  }, [leads])

  const duplicateCount = useMemo(() => leads.filter((l) => l.duplicateOfKey).length, [leads])

  // The lead a "showing duplicates of X" banner should name — may itself be
  // archived (in `leads`) or still active, in which case any linked item's
  // duplicateOfCompany/duplicateOfTitle already carries the display name.
  const duplicateFilterLabel = useMemo(() => {
    if (!duplicateFilterKey) return null
    const linked = leads.find((l) => l.duplicateOfKey === duplicateFilterKey)
    if (linked) return `${linked.duplicateOfTitle} @ ${linked.duplicateOfCompany}`
    const survivor = leads.find((l) => l.normalizedKey === duplicateFilterKey)
    return survivor ? `${survivor.title} @ ${survivor.company}` : duplicateFilterKey
  }, [leads, duplicateFilterKey])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return leads.filter((l) => {
      if (duplicateFilterKey) return l.duplicateOfKey === duplicateFilterKey
      if (statusFilter === 'duplicates') return Boolean(l.duplicateOfKey)
      if (statusFilter !== 'all' && l.status !== statusFilter) return false
      if (!q) return true
      return l.company.toLowerCase().includes(q) || l.title.toLowerCase().includes(q)
    })
  }, [leads, query, statusFilter, duplicateFilterKey])

  // 2026-08-18 perf pass: this table can run to 1000+ rows — rather than
  // mounting every <tr> (the original React.memo-only fix still left a
  // single huge mount/reconcile cost whenever this tab was first selected),
  // only the rows currently scrolled into view (+ overscan) are ever in the
  // DOM. The scroll container is this table's own bounded-height box (see
  // .table-scroll-virtual in App.css), not the outer .contact-priority panel
  // — keeps the virtualizer's scroll math simple (no cross-container offset
  // to account for) at the cost of a second, nested scrollbar.
  const scrollElRef = useRef<HTMLDivElement>(null)
  const rowVirtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => scrollElRef.current,
    estimateSize: () => ESTIMATED_ROW_HEIGHT,
    overscan: 12,
  })

  // Single effect owning all scroll-position decisions for this table —
  // jump-to-target takes priority (mounts a virtualized row that isn't in
  // the current window yet, since a plain getElementById/scrollIntoView
  // can't find a row that doesn't exist in the DOM), falling back to
  // resetting to the top on a search/filter change (so a much shorter
  // filtered list doesn't stay scrolled past its own end). These *must* be
  // one effect, not two: the highlight-clearing effect above can change
  // query/statusFilter/duplicateFilterKey in the very same commit a jump
  // arrives in, and a separately-declared "reset on filter change" effect
  // would then run right after this one's scrollToIndex and silently undo
  // it (2026-08-18 — the same race found in ContactPriorityQueue's
  // virtualization). Deliberately omits `filtered` from the dep array —
  // read fresh via closure regardless, to avoid re-running (and resetting
  // scroll) on every 60s background refresh that changes `leads` without
  // the highlight target or any filter actually changing.
  useEffect(() => {
    if (highlightKey) {
      const index = filtered.findIndex((l) => l.normalizedKey === highlightKey)
      if (index >= 0) {
        rowVirtualizer.scrollToIndex(index, { align: 'center' })
        return
      }
    }
    scrollElRef.current?.scrollTo({ top: 0 })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [highlightKey, query, statusFilter, duplicateFilterKey, rowVirtualizer])

  if (!leads.length) {
    return <p className="empty-hint">No decided (skipped/rejected/etc.) leads on file yet.</p>
  }

  return (
    <div>
      <p className="hint-line">
        Leads off the active funnel above — already decided against, closed out, or already applied
        (tracking a submitted posting through interviewing/offer/etc. instead) — still browsable here
        for their stored message history.
      </p>
      <div className="archive-controls">
        <input
          className="archive-search"
          type="search"
          placeholder="Search company or title…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="filter-bar">
          <button
            type="button"
            className={`filter-chip ${statusFilter === 'all' && !duplicateFilterKey ? 'active' : ''}`}
            onClick={() => {
              setStatusFilter('all')
              setDuplicateFilterKey(null)
            }}
          >
            All <span className="filter-count">{leads.length}</span>
          </button>
          {Object.entries(STATUS_LABELS)
            .filter(([id]) => statusCounts[id])
            .map(([id, label]) => (
              <button
                key={id}
                type="button"
                className={`filter-chip ${statusFilter === id && !duplicateFilterKey ? 'active' : ''}`}
                onClick={() => {
                  setStatusFilter(id)
                  setDuplicateFilterKey(null)
                }}
              >
                {label} <span className="filter-count">{statusCounts[id]}</span>
              </button>
            ))}
          {duplicateCount > 0 && (
            <button
              type="button"
              className={`filter-chip ${statusFilter === 'duplicates' && !duplicateFilterKey ? 'active' : ''}`}
              onClick={() => {
                setStatusFilter('duplicates')
                setDuplicateFilterKey(null)
              }}
              title="Every lead skipped specifically because it duplicated another lead"
            >
              Duplicates <span className="filter-count">{duplicateCount}</span>
            </button>
          )}
        </div>
      </div>
      {duplicateFilterKey && (
        <p className="hint-line duplicate-filter-banner">
          Showing {filtered.length} lead(s) skipped as a duplicate of <strong>{duplicateFilterLabel}</strong>.{' '}
          <button type="button" className="link-button" onClick={() => setDuplicateFilterKey(null)}>
            Clear
          </button>
        </p>
      )}
      <div className="table-scroll table-scroll-virtual" ref={scrollElRef}>
        <table>
          <thead>
            <tr>
              <th>Company</th>
              <th>Title</th>
              <th>Status</th>
              <th>Duplicate of</th>
              <th>Decided</th>
              <th className="num">Match %</th>
              <th>History</th>
            </tr>
          </thead>
          <tbody>
            {rowVirtualizer.getVirtualItems().length > 0 && (
              <tr aria-hidden="true" style={{ height: rowVirtualizer.getVirtualItems()[0].start }}>
                <td colSpan={7} />
              </tr>
            )}
            {rowVirtualizer.getVirtualItems().map((virtualRow) => {
              const l = filtered[virtualRow.index]
              return (
                <tr
                  key={l.normalizedKey}
                  id={leadAnchorId(l.normalizedKey)}
                  ref={rowVirtualizer.measureElement}
                  data-index={virtualRow.index}
                  className={l.normalizedKey === highlightKey ? 'lead-highlight' : undefined}
                >
                  <td>
                    {l.companyFolderPath ? (
                      <a
                        className="company-link"
                        href={revealFolderUrl(l.companyFolderPath) || '#'}
                        title="Open company folder in Finder"
                      >
                        {l.company}
                      </a>
                    ) : (
                      l.company
                    )}
                  </td>
                  <td>
                    {l.folderPath ? (
                      <a
                        className="title-link"
                        href={revealFolderUrl(l.folderPath) || '#'}
                        title="Open this role's folder in Finder"
                      >
                        {l.title}
                      </a>
                    ) : (
                      l.title
                    )}
                    <DuplicateBadge
                      count={l.duplicateCount}
                      firstDuplicateKey={l.duplicateKeys?.[0]}
                      onView={
                        onViewDuplicates
                          ? () => onViewDuplicates(l.normalizedKey, l.duplicateKeys?.[0])
                          : undefined
                      }
                    />
                  </td>
                  <td>
                    <span className={`stage-chip status-${l.status}`}>
                      {STATUS_LABELS[l.status] || l.status}
                    </span>
                  </td>
                  <td>
                    {l.duplicateOfKey ? (
                      <button
                        type="button"
                        className="link-button duplicate-of-link"
                        title={`Show every lead skipped as a duplicate of ${l.duplicateOfTitle} @ ${l.duplicateOfCompany}`}
                        onClick={() => setDuplicateFilterKey(l.duplicateOfKey!)}
                      >
                        {l.duplicateOfCompany}
                        {l.duplicateOfTitle ? ` — ${l.duplicateOfTitle}` : ''}
                      </button>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td className="muted">{formatDecidedAt(l.decidedAt)}</td>
                  <td className="num">{l.matchPct ?? '—'}</td>
                  <td>
                    {l.commCount > 0 && viewCommunicationsUrl(l.company, l.title) ? (
                      <a
                        className="btn"
                        href={viewCommunicationsUrl(l.company, l.title)!}
                        title={`Export and open full communications ODT (${l.commCount} message${l.commCount === 1 ? '' : 's'})`}
                      >
                        History ({l.commCount})
                      </a>
                    ) : (
                      <span className="muted">No messages</span>
                    )}
                  </td>
                </tr>
              )
            })}
            {rowVirtualizer.getVirtualItems().length > 0 && (
              <tr
                aria-hidden="true"
                style={{
                  height:
                    rowVirtualizer.getTotalSize() -
                    rowVirtualizer.getVirtualItems()[rowVirtualizer.getVirtualItems().length - 1].end,
                }}
              >
                <td colSpan={7} />
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {!filtered.length && <p className="empty-hint">No archived leads match "{query}".</p>}
    </div>
  )
})
