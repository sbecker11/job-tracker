import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArchivedLeadsPanel } from './components/ArchivedLeadsPanel'
import { ContactFilterBar } from './components/ContactFilterBar'
import { ContactPriorityQueue } from './components/ContactPriorityQueue'
import { DecideApplyStage } from './components/DecideApplyStage'
import { DuplicatesSkippedPanel } from './components/DuplicatesSkippedPanel'
import { ScheduleHealthBanner } from './components/ScheduleHealthBanner'
import { scrollToLeadRow } from './lib/links'
import {
  allLeadKeys,
  buildContactPriorityQueue,
  contactQueueCounts,
  decideApplyCount,
  filterContactQueue,
  findKeyForAnchorId,
  locateLeadTab,
  replyAckKey,
  type ContactFilter,
  type ContactPriorityItem,
} from './priorityQueue'
import type { WorkflowPayload } from './types'
import './App.css'

const DATA_URL = '/pending-actions.json'
/** Custom URL helper re-runs render_pending_actions.py (see tools/refresh-pending). */
const REGEN_HREF = 'refreshpending://run?no_open=1'
/** Custom URL helper runs comms_fast_cycle.py (see tools/reply-sent). */
const REPLY_SENT_HREF = 'replysent://run'
/** Custom URL helper runs triage_imap_now.py (see tools/triage-imap-now). */
const TRIAGE_IMAP_HREF = 'triageimap://run'
/** Max time to keep the spinner if the helper never finishes / isn't installed. */
const REGEN_TIMEOUT_MS = 120_000
/** Full mailbox tick is slower than render-only; allow ~3 minutes. */
const REPLY_SENT_TIMEOUT_MS = 180_000
/** Full JD-resolve + LLM-extract + LLM-score + generate for one message can
 * run longer than the lightweight Reply-sent scan; allow ~4 minutes. */
const TRIAGE_IMAP_TIMEOUT_MS = 240_000
const REGEN_POLL_MS = 1500
/** How often to silently re-fetch pending-actions.json in the background
 * (see the effect that uses this below). Faster than comms_fast_cycle.py's
 * own ~3-minute LaunchAgent cadence — this is just a cheap static-file GET,
 * not an LLM-billed step, so polling a bit ahead of "how often the backend
 * could realistically have new data" is fine and keeps a left-open tab
 * from ever drifting more than ~1 minute stale. */
const BACKGROUND_REFRESH_MS = 60_000
/** Inbound message ids whose Reply sent already completed (survives refresh). */
const REPLY_SENT_ACK_STORAGE_KEY = 'pending-actions.replySentAck'

function loadReplySentAcks(): Record<string, true> {
  try {
    const raw = localStorage.getItem(REPLY_SENT_ACK_STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Record<string, unknown>
    const out: Record<string, true> = {}
    for (const [k, v] of Object.entries(parsed)) {
      if (v) out[k] = true
    }
    return out
  } catch {
    return {}
  }
}

function saveReplySentAcks(acks: Record<string, true>) {
  try {
    localStorage.setItem(REPLY_SENT_ACK_STORAGE_KEY, JSON.stringify(acks))
  } catch {
    /* ignore quota / private mode */
  }
}

async function fetchWorkflow(): Promise<WorkflowPayload> {
  const res = await fetch(`${DATA_URL}?_=${Date.now()}`, { cache: 'no-store' })
  if (!res.ok) {
    throw new Error(
      `${res.status} loading ${DATA_URL}. Run: python scripts/render_pending_actions.py --no-rescore`,
    )
  }
  return res.json() as Promise<WorkflowPayload>
}

/** "last generated HH:MM:SS ago", ticking live off `nowMs`. */
function formatGeneratedAgo(generatedAt: string | undefined, nowMs: number): string {
  const then = generatedAt ? new Date(generatedAt).getTime() : NaN
  if (!generatedAt || Number.isNaN(then)) return 'last generated —'
  const totalSeconds = Math.max(0, Math.floor((nowMs - then) / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return `last generated ${pad(hours)}:${pad(minutes)}:${pad(seconds)} ago`
}

/** recruiting-automation's run_cycle.sh (which calls render_pending_actions.py)
 * is launchd-scheduled hourly (`StartInterval=3600` in install.sh) — this is
 * an estimate assuming that schedule stayed on since the last render, not a
 * guarantee (it no-ops during a HALT/expiry window). */
const AUTO_GENERATE_INTERVAL_MS = 3600_000

function formatNextAutoGenerate(generatedAt: string | undefined, nowMs: number): string {
  const then = generatedAt ? new Date(generatedAt).getTime() : NaN
  if (!generatedAt || Number.isNaN(then)) return 'next auto-generate —'
  const remainingMs = then + AUTO_GENERATE_INTERVAL_MS - nowMs
  if (remainingMs <= 0) return 'next auto-generate due now'
  const totalSeconds = Math.floor(remainingMs / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return `next auto-generate in ${pad(hours)}:${pad(minutes)}:${pad(seconds)}`
}

function fireCustomUrl(href: string) {
  // Prefer a hidden iframe so the Vite tab is not navigated away by the
  // custom URL scheme (window.location.href can unload the SPA).
  const iframe = document.createElement('iframe')
  iframe.style.display = 'none'
  iframe.src = href
  document.body.appendChild(iframe)
  window.setTimeout(() => iframe.remove(), 5_000)
}

export default function App() {
  const [data, setData] = useState<WorkflowPayload | null>(null)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState<ContactFilter>('all')
  const [regenerating, setRegenerating] = useState(false)
  const [replyScanningId, setReplyScanningId] = useState('')
  /** Ack key captured at click — locked only after scan finishes. */
  const [replyScanningAckKey, setReplyScanningAckKey] = useState('')
  /** Inbound ids already acknowledged after a successful Reply sent scan. */
  const [replySentDone, setReplySentDone] = useState<Record<string, true>>(loadReplySentAcks)
  /** True while a "Check inbox now" (triageimap://run) triage is in flight. */
  const [triagingImap, setTriagingImap] = useState(false)
  /** Lead keys present just before firing triageimap://run — captured
   * synchronously at click time so the poll loop below can diff against it
   * once the refreshed JSON arrives and find whichever lead is new. */
  const triageImapBeforeKeysRef = useRef<Set<string>>(new Set())
  /** Ticks every second so the "last generated ... ago" clock stays live. */
  const [nowMs, setNowMs] = useState(() => Date.now())
  /** Set when a "N duplicates" badge is clicked — every duplicate group
   * still renders (see DuplicatesSkippedPanel), this just tracks which
   * group to auto-expand + flash so it's easy to spot among the rest. */
  const [duplicatesFocusKey, setDuplicatesFocusKey] = useState<string | null>(null)
  /** Set after jumping from a duplicate's "Go to this lead" link back to
   * the survivor's row — briefly flashes + scrolls to it. */
  const [highlightKey, setHighlightKey] = useState<string | null>(null)

  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [])

  useEffect(() => {
    let cancelled = false
    fetchWorkflow()
      .then((payload) => {
        if (!cancelled) {
          setData(payload)
          setError('')
        }
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message || String(err))
      })
    return () => {
      cancelled = true
    }
  }, [])

  /** Restore a lead's location from a `#lead-...` URL hash that's already
   * present when the page first loads — e.g. a pasted/bookmarked link a
   * previous "Duplicate of" / "Go to this lead" click produced. Those
   * click handlers (onJumpToSurvivor/onViewDuplicates below) only ever
   * worked while the app was already mounted; a *fresh* navigation
   * straight to that same URL had no equivalent restoration logic, which
   * turned out to be the real explanation behind "the duplicate-of link's
   * address does nothing" reports — they were reloading/pasting the URL,
   * not clicking inside an already-open page, and the browser's native
   * "jump to this id" only fires once at initial load, before React has
   * rendered the target row (or before the right tab/panel is even open).
   * Runs once, the first time `data` loads — not on every later
   * background refresh (which would otherwise yank the user back here
   * every 60s; see BACKGROUND_REFRESH_MS above). */
  const initialHashHandledRef = useRef(false)
  useEffect(() => {
    if (!data || initialHashHandledRef.current) return
    initialHashHandledRef.current = true
    const hash = window.location.hash
    if (!hash.startsWith('#lead-')) return
    const key = findKeyForAnchorId(data, hash.slice(1))
    if (!key) return
    const tab = locateLeadTab(data, key)
    if (!tab) return
    setHighlightKey(key)
    setFilter(tab)
    scrollToLeadRow(key)
  }, [data])

  useEffect(() => {
    if (!regenerating && !replyScanningId && !triagingImap) return
    const startedAt = data?.generatedAt || ''
    const scanningAckKey = replyScanningAckKey
    const wasReplyScan = Boolean(replyScanningId)
    const wasImapTriage = triagingImap
    const beforeKeys = wasImapTriage ? triageImapBeforeKeysRef.current : null
    const timeoutMs = wasImapTriage
      ? TRIAGE_IMAP_TIMEOUT_MS
      : wasReplyScan
        ? REPLY_SENT_TIMEOUT_MS
        : REGEN_TIMEOUT_MS
    let cancelled = false
    const deadline = Date.now() + timeoutMs

    const tick = async () => {
      if (cancelled) return
      try {
        const payload = await fetchWorkflow()
        if (cancelled) return
        if (payload.generatedAt && payload.generatedAt !== startedAt) {
          setData(payload)
          setError('')
          setRegenerating(false)
          setReplyScanningId('')
          setReplyScanningAckKey('')
          setTriagingImap(false)
          if (wasReplyScan && scanningAckKey) {
            setReplySentDone((prev) => {
              if (prev[scanningAckKey]) return prev
              const next: Record<string, true> = { ...prev, [scanningAckKey]: true }
              saveReplySentAcks(next)
              return next
            })
          }
          if (wasImapTriage) {
            jumpToNewlyProcessedLead(payload, beforeKeys)
          }
          return
        }
      } catch {
        /* keep spinning until timeout — mid-write 404s are possible */
      }
      if (Date.now() >= deadline) {
        if (!cancelled) {
          setRegenerating(false)
          setReplyScanningId('')
          setReplyScanningAckKey('')
          setTriagingImap(false)
          setError(
            wasImapTriage
              ? 'Check inbox now timed out — is tools/triage-imap-now installed? Or run: python scripts/triage_imap_now.py'
              : wasReplyScan
                ? 'Reply sent timed out — is tools/reply-sent installed? Or run: recruiting-automation/run_comms_fast.sh'
                : 'Regenerate timed out — is tools/refresh-pending installed? Or run: python scripts/render_pending_actions.py --no-rescore',
          )
        }
        return
      }
      window.setTimeout(tick, REGEN_POLL_MS)
    }

    const id = window.setTimeout(tick, REGEN_POLL_MS)
    return () => {
      cancelled = true
      window.clearTimeout(id)
    }
  }, [regenerating, replyScanningId, replyScanningAckKey, triagingImap, data?.generatedAt])

  useEffect(() => {
    if (!highlightKey) return
    // Kept in sync with .lead-highlight's animation-duration in App.css —
    // see that rule's 2026-08-18 comment for why this is 4.5s, not 2.5s.
    const id = window.setTimeout(() => setHighlightKey(null), 4500)
    return () => window.clearTimeout(id)
  }, [highlightKey])

  const busy = regenerating || Boolean(replyScanningId) || triagingImap

  // Background auto-refresh (2026-08-18): pending-actions.json is otherwise
  // only fetched once on mount, then again whenever a button-triggered
  // action's own polling loop finishes — so a tab left open across an
  // automation HALT (or its later recovery) can silently show stale
  // scheduleHealth/lead data for hours until the user manually reloads.
  // Paused while `busy`, so it never races the action-specific polling
  // loop above (which does its own fetch-and-diff on a tighter cadence).
  useEffect(() => {
    if (regenerating || replyScanningId || triagingImap) return
    const id = window.setInterval(() => {
      fetchWorkflow()
        .then((payload) => {
          setData(payload)
          setError('')
        })
        .catch(() => {
          /* silent — a transient blip shouldn't interrupt the page; the next tick retries */
        })
    }, BACKGROUND_REFRESH_MS)
    return () => window.clearInterval(id)
  }, [regenerating, replyScanningId, triagingImap])

  /** "N duplicates" badge on any lead row → switch to the Duplicates
   * skipped tab (still showing every group, not just this one), auto-expand
   * this lead's group, and scroll straight to its first duplicate's row.
   *
   * useCallback (2026-08-18 perf pass): this is handed down as a prop to
   * the now-memoized ContactPriorityQueue/DecideApplyStage/ArchivedLeadsPanel
   * (each of which can run to hundreds/thousands of rows) — without a stable
   * identity here, App's 1-second `nowMs` tick would hand those components a
   * "new" callback every render and defeat the memo, re-diffing the entire
   * list every second regardless of whether anything it shows changed. */
  const onViewDuplicates = useCallback((normalizedKey: string, firstDuplicateKey?: string) => {
    setFilter('duplicates_skipped')
    setDuplicatesFocusKey(normalizedKey)
    setHighlightKey(firstDuplicateKey ?? null)
    if (firstDuplicateKey) scrollToLeadRow(firstDuplicateKey)
  }, [])

  /** "Go to this lead" link on a duplicate's card (Duplicates skipped tab)
   * → switch back to wherever the survivor actually lives and flash it.
   * The survivor may have itself since been fully decided (applied →
   * hired/rejected/etc.) and moved out of every active-funnel bucket into
   * the "Archived / decided leads" tab — a real ContactFilter tab (see its
   * 2026-08-18 comment in priorityQueue.ts), so this is just setFilter
   * like any other tab now, not a separately-opened <details>. */
  const onJumpToSurvivor = useCallback((normalizedKey: string) => {
    if (!data) return
    const tab = locateLeadTab(data, normalizedKey)
    if (!tab) {
      setError(
        'That lead is no longer tracked in this dashboard at all (removed from the database) — nothing to jump to.',
      )
      return
    }
    setDuplicatesFocusKey(null)
    setHighlightKey(normalizedKey)
    setFilter(tab)
    scrollToLeadRow(normalizedKey)
  }, [data])

  const onRegenerate = () => {
    if (busy) return
    setError('')
    setRegenerating(true)
    fireCustomUrl(REGEN_HREF)
  }

  /** Once triageimap://run's refreshed JSON lands, diff its lead set against
   * the snapshot captured at click time and jump straight to whichever lead
   * is newly present — wherever it landed (Contact priority, Decide/apply,
   * or Archived), same locate+scroll+flash machinery as onJumpToSurvivor. */
  const jumpToNewlyProcessedLead = (payload: WorkflowPayload, beforeKeys: Set<string> | null) => {
    const afterKeys = allLeadKeys(payload)
    const newKeys = beforeKeys ? [...afterKeys].filter((k) => !beforeKeys.has(k)) : []
    if (!newKeys.length) {
      setError(
        'Checked shawn.becker@spexture.com — no new lead appeared. The message may not have arrived ' +
          'yet, or it was classified as noise/rejection with nothing to track.',
      )
      return
    }
    const key = newKeys[0]
    const tab = locateLeadTab(payload, key)
    setDuplicatesFocusKey(null)
    setHighlightKey(key)
    if (tab) setFilter(tab)
    scrollToLeadRow(key)
  }

  const onTriageImapNow = () => {
    if (busy) return
    setError('')
    triageImapBeforeKeysRef.current = data ? allLeadKeys(data) : new Set()
    setTriagingImap(true)
    fireCustomUrl(TRIAGE_IMAP_HREF)
  }

  const onReplySent = useCallback(
    (item: ContactPriorityItem) => {
      const ackKey = replyAckKey(item)
      if (busy || replySentDone[ackKey]) return
      setError('')
      setReplyScanningId(item.id)
      setReplyScanningAckKey(ackKey)
      fireCustomUrl(REPLY_SENT_HREF)
    },
    [busy, replySentDone],
  )

  const priorityAll = useMemo(
    () => (data ? buildContactPriorityQueue(data) : []),
    [data],
  )
  const counts = useMemo(() => contactQueueCounts(priorityAll), [priorityAll])
  const filtered = useMemo(
    () => filterContactQueue(priorityAll, filter),
    [priorityAll, filter],
  )

  const filterOptions = useMemo(() => {
    if (!data) return []
    return [
      {
        id: 'all' as const,
        label: 'All contact',
        count: counts.all,
        hint: 'Every lead needing outbound contact, ranked by attempts then age',
      },
      {
        id: 'clarify' as const,
        label: 'Clarify',
        count: counts.clarify || 0,
        hint: 'Reply now — unreplied recruiter messages rank first',
      },
      {
        id: 'send_resume' as const,
        label: 'Send résumé',
        count: counts.send_resume || 0,
        hint: (() => {
          const ready = data.stages.sendResume.filter((s) => s.packageReady).length
          const total = data.stages.sendResume.length
          if (!total) return 'Send package to the recruiter'
          if (ready === total) return `${ready} package${ready === 1 ? '' : 's'} ready on disk — send now`
          return `${ready}/${total} packages ready on disk — generate missing docs before sending`
        })(),
      },
      {
        id: 'wait_schedule' as const,
        label: 'Wait / schedule',
        count: counts.wait_schedule || 0,
        hint: `Ball in their court — follow-up due after ${data.waitFollowupDays ?? 7} silent days`,
      },
      {
        id: 'decide_apply' as const,
        label: 'Decide / apply',
        count: decideApplyCount(data.stages.decideApply),
        hint: 'Package/review funnel — not contact prioritization',
      },
      {
        id: 'duplicates_skipped' as const,
        label: '🔁 Duplicates skipped',
        count: data.archivedLeads?.filter((l) => l.duplicateOfKey).length ?? 0,
        hint: 'Leads skipped specifically because they duplicated another lead — grouped by survivor',
      },
      {
        id: 'archived' as const,
        label: 'Archived / decided',
        count: data.archivedLeads?.length ?? 0,
        hint: 'Off the active funnel — already decided against, closed out, or already applied',
      },
    ]
  }, [counts, data])

  const filterLabel =
    filterOptions.find((o) => o.id === filter)?.label || 'All contact'

  return (
    <div className="app">
      <header className="top">
        <div>
          <h1>Pending actions</h1>
          <p className="subtitle">
            Contact priority ranks outbound work (attempts, then age). Each row starts with{" "}
            <strong>YOUR ACTION</strong> — the exact next steps for that lead. Stage chips only
            filter the list.
          </p>
        </div>
        <div className="top-meta">
          {data && (
            <>
              <span>{data.totalLeads} leads</span>
              <span>{formatGeneratedAgo(data.generatedAt, nowMs)}</span>
              <span>{formatNextAutoGenerate(data.generatedAt, nowMs)}</span>
            </>
          )}
          <button
            type="button"
            className="btn link regen-btn"
            onClick={onTriageImapNow}
            disabled={busy}
            title="Fully process any new mail at shawn.becker@spexture.com right now (JD resolve, LLM score, package on pursue) instead of waiting for the next tick"
            aria-busy={triagingImap}
          >
            {triagingImap && <span className="regen-spinner" aria-hidden="true" />}
            {triagingImap ? 'Checking inbox…' : 'Check inbox now'}
          </button>
          <button
            type="button"
            className="btn link regen-btn"
            onClick={onRegenerate}
            disabled={busy}
            title="Re-run render_pending_actions.py, then reload JSON in this tab"
            aria-busy={regenerating}
          >
            {regenerating && <span className="regen-spinner" aria-hidden="true" />}
            {regenerating ? 'Regenerating…' : 'Regenerate'}
          </button>
        </div>
      </header>

      {error && (
        <div className="banner error">
          <strong>Problem.</strong> {error}
        </div>
      )}

      {data?.scheduleHealth?.summary && (
        <ScheduleHealthBanner
          level={data.scheduleHealth.level}
          summary={data.scheduleHealth.summary}
          lastOkAtIso={data.scheduleHealth.lastOkAtIso}
          cycleSteps={data.scheduleHealth.cycleSteps}
          nowMs={nowMs}
        />
      )}

      {data && (
        <section className="contact-priority" aria-label="Contact priority">
          <div className="contact-priority-head">
            <div>
              <h2>Contact priority</h2>
              <p className="panel-action">
                {filter === 'duplicates_skipped'
                  ? 'Leads skipped specifically because they duplicated another lead, grouped by the survivor.'
                  : filter === 'archived'
                    ? 'Off the active funnel above — already decided against, closed out, or already applied — still browsable here for their stored message history.'
                    : 'Ranked by recruiter contact attempts, then age. Read YOUR ACTION on each row before using the buttons. Digests / ATS alerts are excluded — use Decide/apply for those.'}
              </p>
            </div>
            {filter !== 'duplicates_skipped' && filter !== 'archived' && (
              <span className="priority-total">
                {filter === 'decide_apply' ? decideApplyCount(data.stages.decideApply) : filtered.length}{' '}
                shown
              </span>
            )}
          </div>

          <ContactFilterBar
            options={filterOptions}
            active={filter}
            onSelect={(id) => {
              // Manually picking a tab (vs. a "N duplicates" badge calling
              // onViewDuplicates) always shows every duplicate group.
              setFilter(id)
              setDuplicatesFocusKey(null)
            }}
          />

          {filter === 'decide_apply' ? (
            <div className="decide-under-priority">
              <p className="hint-line">
                Decide / apply is package and review work — not part of the contact ranking above.
              </p>
              <DecideApplyStage
                data={data.stages.decideApply}
                onViewDuplicates={onViewDuplicates}
                highlightKey={highlightKey}
              />
            </div>
          ) : filter === 'duplicates_skipped' ? (
            <DuplicatesSkippedPanel
              leads={data.archivedLeads ?? []}
              focusSurvivorKey={duplicatesFocusKey}
              highlightKey={highlightKey}
              onClearFocus={() => setDuplicatesFocusKey(null)}
              onJumpToSurvivor={onJumpToSurvivor}
            />
          ) : filter === 'archived' ? (
            <ArchivedLeadsPanel
              leads={data.archivedLeads ?? []}
              onViewDuplicates={onViewDuplicates}
              highlightKey={highlightKey}
            />
          ) : (
            <ContactPriorityQueue
              items={filtered}
              filterLabel={filterLabel}
              onReplySent={onReplySent}
              replySentDone={replySentDone}
              replyScanningId={replyScanningId}
              replyScanBusy={busy}
              onViewDuplicates={onViewDuplicates}
              highlightKey={highlightKey}
            />
          )}
        </section>
      )}

      {!data && !error && <p className="loading">Loading workflow…</p>}

      {/* Fixed, always rendered regardless of `filter` — every tab (Contact
       * priority, Decide/apply, Duplicates skipped, Archived/decided) can
       * run to a very long list, and there's otherwise no way back to the
       * tab bar / header short of scrolling all the way back up by hand.
       * Instant, not smooth — same reasoning as scrollToLeadRow (links.ts):
       * the point is the tab bar becomes clickable immediately, not
       * eventually once a multi-second scroll animation finishes. */}
      <button
        type="button"
        className="back-to-top"
        onClick={() => {
          window.scrollTo({ top: 0, behavior: 'auto' })
          // .contact-priority scrolls internally (max-height + overflow:
          // auto — see App.css) independently of the window, so a long list
          // scrolled deep in-panel left the tab bar/head off-screen even
          // after the window-level scroll above landed at the top. Reset
          // that inner scrollTop too (2026-08-18).
          document.querySelector('.contact-priority')?.scrollTo({ top: 0, behavior: 'auto' })
        }}
        title="Scroll back to the top — makes the tab bar visible and clickable again"
      >
        ↑ Back to top
      </button>
    </div>
  )
}
