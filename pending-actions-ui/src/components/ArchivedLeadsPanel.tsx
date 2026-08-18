import { useEffect, useMemo, useState } from 'react'
import { DuplicateBadge } from './DuplicateBadge'
import { formatDecidedAt, leadAnchorId, revealFolderUrl, viewCommunicationsUrl } from '../lib/links'
import type { ArchivedLead } from '../types'

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

export function ArchivedLeadsPanel({
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
      <div className="table-scroll">
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
            {filtered.map((l) => (
              <tr
                key={l.normalizedKey}
                id={leadAnchorId(l.normalizedKey)}
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
            ))}
          </tbody>
        </table>
      </div>
      {!filtered.length && <p className="empty-hint">No archived leads match "{query}".</p>}
    </div>
  )
}
