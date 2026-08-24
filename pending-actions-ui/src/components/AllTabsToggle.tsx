import { useMemo, useState } from 'react'
import { searchGlobalIndex, type ContactFilter, type GlobalSearchResult } from '../priorityQueue'

const TAB_LABELS: Record<ContactFilter, string> = {
  all: 'All contact',
  clarify: 'Clarify',
  send_resume: 'Send résumé',
  wait_schedule: 'Wait / schedule',
  decide_apply: 'Decide / apply',
  duplicates_skipped: '🔁 Duplicates skipped',
  archived: 'Archived / decided',
}

const STATUS_LABELS: Record<string, string> = {
  skipped: 'Skipped',
  rejected: 'Rejected',
  deleted: 'Deleted',
  unavailable: 'Unavailable',
  hired: 'Hired',
  applied: 'Applied',
  following_up: 'Following up',
  interviewing: 'Interviewing',
  offered: 'Offered',
  accepted: 'Accepted',
  started: 'Started',
  pursue: 'Pursue',
  pass: 'Pass',
  review: 'Review',
}

interface Props {
  /** The current tab's own search box text — read-only here, this toggle
   * doesn't own its own query, just decides whether that text is also
   * matched against every other tab. */
  query: string
  searchIndex: GlobalSearchResult[]
  onJumpToResult: (result: GlobalSearchResult) => void
}

/** Checkbox next to every tab's own search box (2026-08-24, rebuilt after
 * an earlier same-day-ish session's version never made it into the
 * committed source tree) — unchecked, that tab's search behaves exactly as
 * before (local-only). Checked, the same typed text is also matched
 * against every lead anywhere in the dashboard (buildGlobalSearchIndex),
 * with a dropdown of cross-tab hits rendered right below the input so a
 * lead that "isn't here" because it's actually sitting in a different tab
 * is one click away instead of a guessing game through every tab. */
export function AllTabsToggle({ query, searchIndex, onJumpToResult }: Props) {
  const [allTabs, setAllTabs] = useState(false)
  const q = query.trim()
  const results = useMemo(
    () => (allTabs && q ? searchGlobalIndex(searchIndex, q) : []),
    [allTabs, q, searchIndex],
  )

  return (
    <>
      <label className="all-tabs-toggle">
        <input
          type="checkbox"
          checked={allTabs}
          onChange={(e) => setAllTabs(e.target.checked)}
        />
        All tabs
      </label>
      {allTabs && q && (
        <div className="global-search-results">
          {results.length ? (
            <ul>
              {results.map((r) => (
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
                      {TAB_LABELS[r.tab]}
                      {r.status ? ` · ${STATUS_LABELS[r.status] || r.status}` : ''}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="global-search-empty">No matches anywhere for &quot;{q}&quot;.</p>
          )}
        </div>
      )}
    </>
  )
}
