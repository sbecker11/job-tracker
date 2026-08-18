import { useEffect, useState } from 'react'
import type { CycleStepTiming } from '../types'

/** Mirrors recruiting-automation/run_cycle.sh's step sequence (2026-08-18).
 * That script lives in a sibling repo and isn't part of this JSON payload,
 * so the step list/copy here is kept in sync by hand whenever run_cycle.sh
 * changes. `matchPrefix` is checked against each `CycleStepTiming.title`
 * (the step's exact log description) with `startsWith` rather than an
 * exact match, so a parenthetical detail added/reworded on the log-line
 * side (e.g. "(live, LLM fallback default-on)") doesn't break the lookup —
 * only the stable leading text needs to agree. */
const CYCLE_STEPS: { matchPrefix: string; title: string; detail: string }[] = [
  {
    matchPrefix: 'comms-migration: classify personal_hub',
    title: 'Classify — personal_hub',
    detail: 'Classifies new mail on the personal_hub Gmail account (live, with LLM fallback).',
  },
  {
    matchPrefix: 'comms-migration: classify recruiting_funnel',
    title: 'Classify — recruiting_funnel',
    detail:
      'Classifies new mail on shawnbecker.recruiting@gmail.com, plus a bounded Spam-folder sweep for recruiter_job mail.',
  },
  {
    matchPrefix: 'job-tracker: triage_recruiter_inbox',
    title: 'Triage recruiter inbox',
    detail:
      'Scores newly-classified recruiter_job mail (LLM evaluation + fallback extraction), auto-generating a résumé/cover-letter package on a "pursue" verdict.',
  },
  {
    matchPrefix: 'job-tracker: scan_communications',
    title: 'Scan communications',
    detail: 'Matches LinkedIn replies and Sent-folder threads back to existing tracked leads.',
  },
  {
    matchPrefix: 'job-tracker: triage_imap_inbox',
    title: 'Triage IMAP inbox (Spexture)',
    detail: 'Triages the shawn.becker@spexture.com IMAP mailbox — invisible to every Gmail-API step above.',
  },
  {
    matchPrefix: 'job-tracker: process_awaiting_llm_review',
    title: 'Process awaiting LLM review',
    detail: "Runs the full-LLM-review sweep for leads whose rule-based score cleared the gate but never got a real LLM call.",
  },
  {
    matchPrefix: 'job-tracker: resync_labels',
    title: 'Resync labels',
    detail: "Re-syncs each already-triaged message's JobTracker/PURSUE|SKIP|NEEDS_REVIEW Gmail label to its lead's current verdict.",
  },
  {
    matchPrefix: 'job-tracker: render_pending_actions',
    title: 'Render pending actions',
    detail: 'Regenerates the static Pending actions HTML/JSON — the data this page loads.',
  },
  {
    matchPrefix: 'job-tracker: render_contacts',
    title: 'Render contacts',
    detail: 'Regenerates the static contacts-lookup page.',
  },
]

const CYCLE_WORD_RE = /\bcycle\b/

/** "Last successful cycle HH:MM:SS ago." — same live-ticking shape as
 * App.tsx's formatGeneratedAgo, computed client-side off `lastOkAtIso` +
 * `nowMs` instead of the static, server-rendered `summary` string (which
 * is only as fresh as the last JSON regenerate, not actually live). Only
 * used for the "ok" health level — halted/stale/no-data summaries don't
 * have a single elapsed-time phrase in the same place, so they fall back
 * to the server's own wording untouched. */
function liveOkSummary(lastOkAtIso: string | undefined, nowMs: number): string | null {
  const then = lastOkAtIso ? new Date(lastOkAtIso).getTime() : NaN
  if (!lastOkAtIso || Number.isNaN(then)) return null
  const totalSeconds = Math.max(0, Math.floor((nowMs - then) / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return `Last successful cycle ${pad(hours)}:${pad(minutes)}:${pad(seconds)} ago.`
}

function findTiming(cycleSteps: CycleStepTiming[] | undefined, matchPrefix: string): CycleStepTiming | undefined {
  return cycleSteps?.find((s) => s.title.startsWith(matchPrefix))
}

function formatSeconds(seconds: number | null): string {
  if (seconds == null) return '—'
  if (seconds < 1) return '<1s'
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const total = Math.round(seconds)
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}m ${s}s`
}

function formatTiming(timing: CycleStepTiming | undefined): string {
  if (!timing) return '—'
  if (timing.status === 'incomplete') return 'still running…'
  const duration = formatSeconds(timing.seconds)
  if (timing.status === 'ok') return duration
  if (timing.status === 'failed') return `${duration} (failed)`
  return `${duration} (timed out)`
}

function CycleStepsModal({
  cycleSteps,
  onClose,
}: {
  cycleSteps: CycleStepTiming[] | undefined
  onClose: () => void
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="Recruiting-automation cycle steps"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h2>One cycle, in order</h2>
          <button type="button" className="link-button modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <p className="hint-line">
          Every hourly tick of recruiting-automation's <code>run_cycle.sh</code> runs these steps in sequence — the
          first one that fails halts the rest until the next tick (or a manual restart). Times are from the most
          recent cycle's log{!cycleSteps?.length && ' (none found yet)'}.
        </p>
        <ol className="cycle-steps-list">
          {CYCLE_STEPS.map((step) => {
            const timing = findTiming(cycleSteps, step.matchPrefix)
            return (
              <li key={step.matchPrefix} className={timing && timing.status !== 'ok' ? `cycle-step-${timing.status}` : ''}>
                <div className="cycle-step-row">
                  <span className="cycle-step-title">{step.title}</span>
                  <span className="cycle-step-time">{formatTiming(timing)}</span>
                </div>
                <p className="cycle-step-detail">{step.detail}</p>
              </li>
            )
          })}
        </ol>
      </div>
    </div>
  )
}

export function ScheduleHealthBanner({
  level,
  summary,
  lastOkAtIso,
  cycleSteps,
  nowMs,
}: {
  level?: string
  summary: string
  lastOkAtIso?: string
  cycleSteps?: CycleStepTiming[]
  nowMs: number
}) {
  const [open, setOpen] = useState(false)

  const displaySummary = (level === 'ok' && liveOkSummary(lastOkAtIso, nowMs)) || summary

  const match = CYCLE_WORD_RE.exec(displaySummary)
  const content = !match ? (
    displaySummary
  ) : (
    <>
      {displaySummary.slice(0, match.index)}
      <button type="button" className="link-button cycle-word-trigger" onClick={() => setOpen(true)}>
        cycle
      </button>
      {displaySummary.slice(match.index + match[0].length)}
    </>
  )

  return (
    <>
      <div className={`banner health-${level || 'info'}`}>{content}</div>
      {open && <CycleStepsModal cycleSteps={cycleSteps} onClose={() => setOpen(false)} />}
    </>
  )
}
