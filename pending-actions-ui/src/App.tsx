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

export default function App() {
  const [data, setData] = useState<WorkflowPayload | null>(null)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState<ContactFilter>('all')

  useEffect(() => {
    let cancelled = false
    fetch(DATA_URL)
      .then(async (res) => {
        if (!res.ok) {
          throw new Error(
            `${res.status} loading ${DATA_URL}. Run: python scripts/render_pending_actions.py --no-rescore`,
          )
        }
        return res.json() as Promise<WorkflowPayload>
      })
      .then((payload) => {
        if (cancelled) return
        setData(payload)
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message || String(err))
      })
    return () => {
      cancelled = true
    }
  }, [])

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
        hint: 'Reply with clarifiers — draft attached to each lead',
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
        hint: 'Ball in their court',
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
          <a className="btn link" href="refreshpending://run?no_open=1" title="Regenerate JSON + HTML">
            Regenerate
          </a>
        </div>
      </header>

      {error && (
        <div className="banner error">
          <strong>No workflow data.</strong> {error}
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
