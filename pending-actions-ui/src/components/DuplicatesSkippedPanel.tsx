import { memo, useMemo, useState } from 'react'
import { AllTabsToggle } from './AllTabsToggle'
import { ManageLeadStatus } from './ManageLeadStatus'
import { formatDecidedAt, leadAnchorHref, leadAnchorId, revealFolderUrl, viewCommunicationsUrl } from '../lib/links'
import type { GlobalSearchResult } from '../priorityQueue'
import type { ArchivedLead } from '../types'

interface DuplicateGroup {
  survivorKey: string
  survivorCompany: string
  survivorTitle: string
  duplicates: ArchivedLead[]
  latestSkippedAt: string
}

/** Groups every archived lead that carries a `duplicateOfKey` (see
 * store.mark_duplicate) by the survivor it duplicates — one card per
 * survivor lead, holding every recruiter/vendor copy that got skipped
 * because of it.
 *
 * 2026-08-17: promoted from a collapsible folder nested under "Archived /
 * decided leads" to its own top-level "Duplicates skipped" tab (see
 * ContactFilter in priorityQueue.ts) — the nested-folder version required
 * scrolling into a closed <details>, which turned out unreliable to do
 * from a "N duplicates" badge elsewhere on the page. A real tab switch
 * (App.tsx's onViewDuplicates/onJumpToSurvivor) replaced that entirely. */
function groupDuplicates(leads: ArchivedLead[]): DuplicateGroup[] {
  const groups = new Map<string, DuplicateGroup>()
  for (const l of leads) {
    if (!l.duplicateOfKey) continue
    let g = groups.get(l.duplicateOfKey)
    if (!g) {
      g = {
        survivorKey: l.duplicateOfKey,
        survivorCompany: l.duplicateOfCompany || '(unknown company)',
        survivorTitle: l.duplicateOfTitle || '(unknown title)',
        duplicates: [],
        latestSkippedAt: '',
      }
      groups.set(l.duplicateOfKey, g)
    }
    g.duplicates.push(l)
    if (!g.latestSkippedAt || (l.decidedAt || '') > g.latestSkippedAt) {
      g.latestSkippedAt = l.decidedAt || ''
    }
  }
  return Array.from(groups.values()).sort((a, b) => (b.latestSkippedAt || '').localeCompare(a.latestSkippedAt || ''))
}

function DuplicateGroupCard({
  group,
  defaultOpen,
  focused,
  highlightKey,
  onJumpToSurvivor,
}: {
  group: DuplicateGroup
  defaultOpen: boolean
  focused: boolean
  highlightKey?: string | null
  onJumpToSurvivor?: (key: string) => void
}) {
  return (
    <details className={`dup-group${focused ? ' dup-group-focused' : ''}`} open={defaultOpen}>
      <summary className="dup-group-summary">
        <span className="dup-group-icon" aria-hidden="true">
          📁
        </span>
        <span className="dup-group-title">
          {group.survivorTitle} <span className="muted">@ {group.survivorCompany}</span>
        </span>
        <span className="pill">{group.duplicates.length}</span>
        {onJumpToSurvivor && (
          <a
            className="link-button dup-jump-link"
            href={leadAnchorHref(group.survivorKey)}
            title="Jump to this lead wherever it currently lives"
            onClick={(e) => {
              e.preventDefault()
              e.stopPropagation()
              onJumpToSurvivor(group.survivorKey)
            }}
          >
            Go to this lead →
          </a>
        )}
      </summary>
      <table className="dup-group-table">
        <thead>
          <tr>
            <th>Company</th>
            <th>Title</th>
            <th>Status</th>
            <th>Skipped</th>
            <th>Duplicate of</th>
            <th>History</th>
          </tr>
        </thead>
        <tbody>
          {group.duplicates.map((d) => (
            <tr
              key={d.normalizedKey}
              id={leadAnchorId(d.normalizedKey)}
              className={d.normalizedKey === highlightKey ? 'lead-highlight' : undefined}
            >
              <td>
                {d.companyFolderPath ? (
                  <a
                    className="company-link"
                    href={revealFolderUrl(d.companyFolderPath) || '#'}
                    title="Open company folder in Finder"
                  >
                    {d.company}
                  </a>
                ) : (
                  d.company
                )}
              </td>
              <td>
                {d.folderPath ? (
                  <a className="title-link" href={revealFolderUrl(d.folderPath) || '#'} title="Open this role's folder in Finder">
                    {d.title}
                  </a>
                ) : (
                  d.title
                )}
              </td>
              <td>
                <span className={`stage-chip status-${d.status}`}>{d.status}</span>
              </td>
              <td className="muted">{formatDecidedAt(d.decidedAt)}</td>
              <td>
                {onJumpToSurvivor ? (
                  <a
                    className="link-button duplicate-of-link"
                    href={leadAnchorHref(group.survivorKey)}
                    title={`Go to ${group.survivorTitle} @ ${group.survivorCompany}`}
                    onClick={(e) => {
                      e.preventDefault()
                      onJumpToSurvivor(group.survivorKey)
                    }}
                  >
                    {group.survivorCompany} — {group.survivorTitle}
                  </a>
                ) : (
                  <span className="muted">
                    {group.survivorCompany} — {group.survivorTitle}
                  </span>
                )}
              </td>
              <td>
                {d.commCount > 0 && viewCommunicationsUrl(d.company, d.title) ? (
                  <a
                    className="btn"
                    href={viewCommunicationsUrl(d.company, d.title)!}
                    title={`Export and open full communications ODT (${d.commCount} message${d.commCount === 1 ? '' : 's'})`}
                  >
                    History ({d.commCount})
                  </a>
                ) : (
                  <span className="muted">No messages</span>
                )}
                <ManageLeadStatus normalizedKey={d.normalizedKey} currentStatus={d.status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  )
}

// Memoized — see 2026-08-18 perf pass note on ContactPriorityQueue.
export const DuplicatesSkippedPanel = memo(function DuplicatesSkippedPanel({
  leads,
  focusSurvivorKey,
  highlightKey,
  onClearFocus,
  onJumpToSurvivor,
  searchIndex = [],
  onJumpToResult,
  allTabsSearch = false,
  onAllTabsSearchChange,
}: {
  leads: ArchivedLead[]
  /** Which group to auto-expand + visually mark as "jumped to" — every
   * group still renders below regardless (2026-08-17: filtering down to
   * just this one hid the rest of the duplicate sets, which defeated the
   * point of browsing them). */
  focusSurvivorKey?: string | null
  /** Row (inside the focused group) to flash on arrival. */
  highlightKey?: string | null
  onClearFocus?: () => void
  onJumpToSurvivor?: (key: string) => void
  /** Cross-tab search — see AllTabsToggle. */
  searchIndex?: GlobalSearchResult[]
  onJumpToResult?: (result: GlobalSearchResult) => void
  /** Lifted to App.tsx (2026-08-26) — see AllTabsToggle's comment. */
  allTabsSearch?: boolean
  onAllTabsSearchChange?: (value: boolean) => void
}) {
  const [query, setQuery] = useState('')

  const groups = useMemo(() => groupDuplicates(leads), [leads])
  const filteredGroups = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return groups
    return groups.filter(
      (g) =>
        g.survivorCompany.toLowerCase().includes(q) ||
        g.survivorTitle.toLowerCase().includes(q) ||
        g.duplicates.some(
          (d) =>
            d.company.toLowerCase().includes(q) ||
            d.title.toLowerCase().includes(q) ||
            (d.recruiterName?.toLowerCase().includes(q) ?? false) ||
            (d.recruiterEmail?.toLowerCase().includes(q) ?? false) ||
            (d.recruiterPhone?.toLowerCase().includes(q) ?? false),
        ),
    )
  }, [groups, query])

  const totalDuplicates = useMemo(() => groups.reduce((sum, g) => sum + g.duplicates.length, 0), [groups])

  if (!groups.length) {
    return (
      <p className="empty-hint">
        No duplicate leads linked yet — see <code>store.mark_duplicate()</code> /{' '}
        <code>list_leads.py --mark-duplicate-of</code>.
      </p>
    )
  }

  const focusedGroup = focusSurvivorKey ? groups.find((g) => g.survivorKey === focusSurvivorKey) : null

  return (
    <div>
      <p className="hint-line">
        {totalDuplicates} lead(s) skipped as a duplicate of another, grouped by the survivor they duplicate — the
        one actually pursued (or itself already decided). Expand a folder to see every recruiter/vendor copy and
        pull up its message history.
      </p>
      {focusSurvivorKey && (
        <p className="hint-line duplicate-filter-banner">
          Jumped to duplicates of{' '}
          <strong>
            {focusedGroup ? `${focusedGroup.survivorTitle} @ ${focusedGroup.survivorCompany}` : focusSurvivorKey}
          </strong>{' '}
          — its folder is expanded and highlighted below; every other duplicate set is still shown too.{' '}
          {onClearFocus && (
            <button type="button" className="link-button" onClick={onClearFocus}>
              Clear
            </button>
          )}
        </p>
      )}
      <div className="archive-controls">
        <input
          className="archive-search"
          type="search"
          placeholder="Search survivor/duplicate company, title, recruiter, email, or phone…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {onJumpToResult && onAllTabsSearchChange && (
          <AllTabsToggle
            query={query}
            searchIndex={searchIndex}
            onJumpToResult={onJumpToResult}
            allTabs={allTabsSearch}
            onAllTabsChange={onAllTabsSearchChange}
          />
        )}
      </div>
      <div className="dup-groups">
        {filteredGroups.map((g, i) => (
          <DuplicateGroupCard
            key={g.survivorKey}
            group={g}
            focused={g.survivorKey === focusSurvivorKey}
            highlightKey={highlightKey}
            defaultOpen={g.survivorKey === focusSurvivorKey || (!focusSurvivorKey && i === 0 && filteredGroups.length <= 3)}
            onJumpToSurvivor={onJumpToSurvivor}
          />
        ))}
      </div>
      {!filteredGroups.length && <p className="empty-hint">No duplicate sets match "{query}".</p>}
    </div>
  )
})
