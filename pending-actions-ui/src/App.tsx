import { useEffect, useMemo, useState } from 'react'
import { ContactFilterBar, decideApplyCount } from './components/ContactFilterBar'
import { ContactPriorityQueue } from './components/ContactPriorityQueue'
import { DecideApplyStage } from './components/DecideApplyStage'
import {
  buildContactPriorityQueue,
  contactQueueCounts,
  filterContactQueue,
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
/** Max time to keep the spinner if the helper never finishes / isn't installed. */
const REGEN_TIMEOUT_MS = 120_000
/** Full mailbox tick is slower than render-only; allow ~3 minutes. */
const REPLY_SENT_TIMEOUT_MS = 180_000
const REGEN_POLL_MS = 1500
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
    if (!regenerating && !replyScanningId) return
    const startedAt = data?.generatedAt || ''
    const scanningAckKey = replyScanningAckKey
    const wasReplyScan = Boolean(replyScanningId)
    const timeoutMs = wasReplyScan ? REPLY_SENT_TIMEOUT_MS : REGEN_TIMEOUT_MS
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
          if (wasReplyScan && scanningAckKey) {
            setReplySentDone((prev) => {
              if (prev[scanningAckKey]) return prev
              const next = { ...prev, [scanningAckKey]: true }
              saveReplySentAcks(next)
              return next
            })
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
          setError(
            wasReplyScan
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
  }, [regenerating, replyScanningId, replyScanningAckKey, data?.generatedAt])

  const busy = regenerating || Boolean(replyScanningId)

  const onRegenerate = () => {
    if (busy) return
    setError('')
    setRegenerating(true)
    fireCustomUrl(REGEN_HREF)
  }

  const onReplySent = (item: ContactPriorityItem) => {
    const ackKey = replyAckKey(item)
    if (busy || replySentDone[ackKey]) return
    setError('')
    setReplyScanningId(item.id)
    setReplyScanningAckKey(ackKey)
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
            <ContactPriorityQueue
              items={filtered}
              filterLabel={filterLabel}
              onReplySent={onReplySent}
              replySentDone={replySentDone}
              replyScanningId={replyScanningId}
              replyScanBusy={busy}
            />
          )}
        </section>
      )}

      {!data && !error && <p className="loading">Loading workflow…</p>}
    </div>
  )
}
