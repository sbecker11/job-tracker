import { leadAnchorHref } from '../lib/links'

/** "N duplicates" link shown next to a lead's company/title wherever it
 * appears (Contact priority queue, Decide/apply, Archived) when other leads
 * have been marked duplicates of it (store.mark_duplicate). A genuine
 * `<a href="#lead-...">` pointing at the first duplicate's row anchor (see
 * lib/links.ts's leadAnchorId) — not just a JS-only button — so it's a real,
 * inspectable, right-click-able link. onClick still does the actual work
 * (switch to the Duplicates skipped tab, focus this lead's group, then
 * scroll), since the target only exists in the DOM once that tab is
 * showing; preventDefault stops the browser from trying (and failing) to
 * jump there before the tab switch has rendered it. */
export function DuplicateBadge({
  count,
  firstDuplicateKey,
  onView,
}: {
  count?: number
  firstDuplicateKey?: string
  onView?: () => void
}) {
  if (!count || !firstDuplicateKey || !onView) return null
  return (
    <a
      className="dup-badge"
      href={leadAnchorHref(firstDuplicateKey)}
      onClick={(e) => {
        e.preventDefault()
        onView()
      }}
      title={`${count} other lead${count === 1 ? '' : 's'} skipped as a duplicate of this one — jump to the Duplicates skipped tab`}
    >
      {count} duplicate{count === 1 ? '' : 's'}
    </a>
  )
}
