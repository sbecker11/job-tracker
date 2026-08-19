/** Shared custom-URL helpers (Finder reveal, communications export) used by
 * both ArchivedLeadsPanel and DuplicateLeadsFolder — kept in one place so the
 * two never drift on how these links get built. */

/** Finder folder open (tools/reveal-folder/). */
export function revealFolderUrl(folderPath?: string): string | null {
  if (!folderPath) return null
  return `revealfolder://reveal?path=${encodeURIComponent(folderPath)}`
}

/** Export + open this lead's job_conversations ODT (tools/view-communications/). */
export function viewCommunicationsUrl(company?: string, title?: string): string | null {
  if (!company?.trim() || !title?.trim()) return null
  return `viewcomms://open?company=${encodeURIComponent(company)}&title=${encodeURIComponent(title)}`
}

export function formatDecidedAt(iso?: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10) || '—'
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

/** Real HTML id for a lead's row, used as a genuine `<a href="#...">`
 * anchor target — every row that can be jumped to (Contact priority,
 * Decide/apply, Archived, and each duplicate row on the Duplicates skipped
 * tab) gets one, via this single shared function, so an href built on one
 * side always matches the id rendered on the other. normalizedKeys look
 * like "company::title", not valid as a raw HTML id, so non-id-safe
 * characters are collapsed to `-`. */
export function leadAnchorId(normalizedKey: string): string {
  return `lead-${normalizedKey.replace(/[^a-zA-Z0-9_-]/g, '-')}`
}

/** `href` for a real anchor link to a lead's row — combine with an onClick
 * that does the actual tab-switch (see App.tsx's onViewDuplicates /
 * onJumpToSurvivor) since the target only exists in the DOM once the
 * right tab is showing; the plain hash href still makes the link a real,
 * inspectable, right-click-able anchor rather than a JS-only button. */
export function leadAnchorHref(normalizedKey: string): string {
  return `#${leadAnchorId(normalizedKey)}`
}

/** Scrolls to and briefly flashes the row for `normalizedKey` — used after
 * switching tabs, or opening the Archived <details> (see App.tsx's
 * onJumpToSurvivor/onViewDuplicates) to land on a lead that's now visible.
 *
 * 2026-08-17: an earlier version of duplicate-lead navigation tried to
 * reach into a *closed* `<details>` element via `scrollIntoView` — that
 * doesn't reliably work (closed <details> content has no layout box until
 * opened, and browsers differ on auto-expanding for fragment navigation).
 * The fix was architectural for the Duplicates-skipped case ("Duplicates
 * skipped" became its own top-level tab instead of a nested folder), but
 * the *target* can still be the "Archived / decided leads" <details> below
 * the tabs (a survivor jump can land there — see locateLeadTab's 'archived'
 * branch), and *that* re-render can take more than one frame to settle:
 * opening it + ArchivedLeadsPanel's own filter-reset effect are two
 * separate state updates, not one. A fixed double-rAF can fire in the gap
 * between them and find a not-yet-laid-out (zero-size) element and
 * silently no-op. Polling for an actually-laid-out element (non-empty
 * getClientRects) up to ~20 frames (~one-third of a second) instead of
 * guessing exactly how many re-renders are involved fixes that without the
 * caller needing to know that stage-and-filter-reset happens at all.
 *
 * 2026-08-18: switched `behavior: 'smooth'` → `'auto'` (instant) after
 * confirming the *actual* bug behind "the Duplicate of link does nothing"
 * — with 1200+ archived rows, the target can be tens of thousands of
 * pixels down the page, and a smooth scroll across that distance took
 * ~2.2s in testing. The `.lead-highlight` flash (App.css) only lasts 2.5s
 * *from click time*, before the scroll even starts — so by the time the
 * page finished crawling down, the highlight had already faded (or was
 * about to), leaving the user staring at an unmarked row indistinguishable
 * from 1000+ others. It scrolled, technically, but looked completely
 * broken. Jumping instantly removes that race entirely regardless of list
 * size or which browser's smooth-scroll timing is in play. Also updates
 * the URL hash (via replaceState, not a real navigation) once landed, so
 * the address bar gives visible confirmation the click did something —
 * restores the "real, inspectable anchor" behavior `leadAnchorHref` was
 * meant to have (bookmarkable, right-click-able) instead of a silently
 * JS-only jump.
 *
 * 2026-08-18 (later same day): the 20-frame (~330ms) polling budget above
 * turned out to be too short for the "survivor already fully decided, so
 * open the 1200+-row Archived panel" path — confirmed live: highlightKey
 * was correctly set (visible as the flashed row once you scrolled/zoomed
 * out to find it yourself) but the *actual* scroll never fired, because
 * that panel's <details> opening plus its own filter-reset re-render of
 * every archived row settled in ~650ms — comfortably past 20 frames'
 * worth of polling, so `tryScroll` silently exhausted its attempts and
 * gave up just short of the row being ready. Switched to a wall-clock
 * deadline (2s) instead of a fixed frame count — rAF cadence isn't
 * guaranteed to be ~16ms (a backgrounded/throttled tab can be much
 * slower), so counting frames was measuring the wrong thing regardless of
 * how many were allowed. */
export function scrollToLeadRow(normalizedKey: string): void {
  const id = leadAnchorId(normalizedKey)
  const deadline = performance.now() + 2_000
  const tryScroll = () => {
    const el = document.getElementById(id)
    if (el && el.getClientRects().length > 0) {
      el.scrollIntoView({ behavior: 'auto', block: 'center' })
      if (window.location.hash !== `#${id}`) {
        window.history.replaceState(null, '', `#${id}`)
      }
      return
    }
    if (performance.now() < deadline) requestAnimationFrame(tryScroll)
  }
  requestAnimationFrame(tryScroll)
}
