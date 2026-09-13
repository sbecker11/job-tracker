import { useMemo } from 'react'
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
  /** Lifted to App.tsx (2026-08-26) — see that checked/onChange pair's own
   * comment for why this can no longer be this component's own
   * `useState`. */
  allTabs: boolean
  onAllTabsChange: (value: boolean) => void
}

/** Checkbox next to every tab's own search box (2026-08-24, rebuilt after
 * an earlier same-day-ish session's version never made it into the
 * committed source tree) — unchecked, that tab's search behaves exactly as
 * before (local-only). Checked, the same typed text is also matched
 * against every lead anywhere in the dashboard (buildGlobalSearchIndex),
 * with a dropdown of cross-tab hits rendered right below the input so a
 * lead that "isn't here" because it's actually sitting in a different tab
 * is one click away instead of a guessing game through every tab.
 *
 * `allTabs` is a controlled prop, not local state (2026-08-26 fix) — each
 * top-level tab (Clarify/Wait/Decide-apply/Duplicates-skipped/Archived)
 * mounts a *different* component that renders its own `<AllTabsToggle>`,
 * so App.tsx's ternary tab switch unmounts the previous one entirely.
 * Local `useState(false)` here meant the checkbox silently reset to
 * unchecked on every tab switch with zero visual cue — confirmed live as
 * the actual explanation behind "I checked All tabs, searched on a
 * different tab, and got zero matches" reports that looked like a search
 * bug but were really this reset. */
export function AllTabsToggle({ query, searchIndex, onJumpToResult, allTabs, onAllTabsChange }: Props) {
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
          onChange={(e) => onAllTabsChange(e.target.checked)}
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
