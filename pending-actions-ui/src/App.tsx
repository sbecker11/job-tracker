import { useEffect, useMemo, useState } from 'react'
import { ContactFilterBar, decideApplyCount } from './components/ContactFilterBar'
import { ContactPriorityQueue } from './components/ContactPriorityQueue'
import { DecideApplyStage } from './components/DecideApplyStage'
import {
  buildContactPriorityQueue,
  contactQueueCounts,
  filterContactQueue,
  type ContactFilter,
} from './priorityQueue'
import type { WorkflowPayload } from './types'
import './App.css'

const DATA_URL = '/pending-actions.json'
/** Custom URL helper re-runs render_pending_actions.py (see tools/refresh-pending). */
const REGEN_HREF = 'refreshpending://run?no_open=1'
/** Custom URL helper runs comms_fast_cycle.py (see tools/reply-sent). */
const REPLY_SENT_HREF = 'replysent://run'
/** Max time to keep the spinner if the helper never finishes / isn't installed. */
const REGEN_TIMEOUT_MS = 120_000
/** Full mailbox tick is slower than render-only; allow ~3 minutes. */
const REPLY_SENT_TIMEOUT_MS = 180_000
const REGEN_POLL_MS = 1500

async function fetchWorkflow(): Promise<WorkflowPayload> {
  const res = await fetch(`${DATA_URL}?_=${Date.now()}`, { cache: 'no-store' })
  if (!res.ok) {
    throw new Error(
      `${res.status} loading ${DATA_URL}. Run: python scripts/render_pending_actions.py --no-rescore`,
    )
  }
  return res.json() as Promise<WorkflowPayload>
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
  const [replyScanning, setReplyScanning] = useState(false)

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

  useEffect(() => {
    if (!regenerating && !replyScanning) return
    const startedAt = data?.generatedAt || ''
    const timeoutMs = replyScanning ? REPLY_SENT_TIMEOUT_MS : REGEN_TIMEOUT_MS
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
          setReplyScanning(false)
          return
        }
      } catch {
        /* keep spinning until timeout — mid-write 404s are possible */
      }
      if (Date.now() >= deadline) {
        if (!cancelled) {
          setRegenerating(false)
          setReplyScanning(false)
          setError(
            replyScanning
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
  }, [regenerating, replyScanning, data?.generatedAt])

  const busy = regenerating || replyScanning

  const onRegenerate = () => {
    if (busy) return
    setError('')
    setRegenerating(true)
    fireCustomUrl(REGEN_HREF)
  }

  const onReplySent = () => {
    if (busy) return
    setError('')
    setReplyScanning(true)
    fireCustomUrl(REPLY_SENT_HREF)
  }

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
        hint: 'Send package to the recruiter',
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
              <span>Generated {data.generatedAt || '—'}</span>
            </>
          )}
          <button
            type="button"
            className="btn link regen-btn"
            onClick={onReplySent}
            disabled={busy}
            title="After you BCC yourself on a LinkedIn reply: scan Sent + refresh so the card can move to Wait"
            aria-busy={replyScanning}
          >
            {replyScanning && <span className="regen-spinner" aria-hidden="true" />}
            {replyScanning ? 'Scanning…' : 'Reply sent'}
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
        <div className={`banner health-${data.scheduleHealth.level || 'info'}`}>
          {data.scheduleHealth.summary}
        </div>
      )}

      {data && (
        <section className="contact-priority" aria-label="Contact priority">
          <div className="contact-priority-head">
            <div>
              <h2>Contact priority</h2>
              <p className="panel-action">
                Ranked by recruiter contact attempts, then age. Read YOUR ACTION on each row before
                using the buttons. Digests / ATS alerts are excluded — use Decide/apply for those.
              </p>
            </div>
            <span className="priority-total">
              {filter === 'decide_apply' ? decideApplyCount(data.stages.decideApply) : filtered.length}{' '}
              shown
            </span>
          </div>

          <ContactFilterBar options={filterOptions} active={filter} onSelect={setFilter} />

          {filter === 'decide_apply' ? (
            <div className="decide-under-priority">
              <p className="hint-line">
                Decide / apply is package and review work — not part of the contact ranking above.
              </p>
              <DecideApplyStage data={data.stages.decideApply} folderRoot={data.folderRoot} />
            </div>
          ) : (
            <ContactPriorityQueue items={filtered} filterLabel={filterLabel} />
          )}
        </section>
      )}

      {!data && !error && <p className="loading">Loading workflow…</p>}
    </div>
  )
}
