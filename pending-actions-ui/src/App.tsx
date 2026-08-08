import { useEffect, useMemo, useState } from 'react'
import { ClarifyQueue } from './components/ClarifyQueue'
import { DecideApplyStage } from './components/DecideApplyStage'
import { SendResumeQueue } from './components/SendResumeQueue'
import { StagePipeline } from './components/StagePipeline'
import { WaitQueue } from './components/WaitQueue'
import type { PipelineStageId, WorkflowPayload } from './types'
import { STAGE_ORDER } from './types'
import './App.css'

const DATA_URL = '/pending-actions.json'

function pickDefaultStage(data: WorkflowPayload): PipelineStageId {
  for (const id of STAGE_ORDER) {
    const stage = data.pipeline.find((s) => s.id === id)
    if (stage && stage.count > 0) return id
  }
  return 'clarify'
}

export default function App() {
  const [data, setData] = useState<WorkflowPayload | null>(null)
  const [error, setError] = useState('')
  const [active, setActive] = useState<PipelineStageId>('clarify')

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
        setActive(pickDefaultStage(payload))
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message || String(err))
      })
    return () => {
      cancelled = true
    }
  }, [])

  const stageBody = useMemo(() => {
    if (!data) return null
    switch (active) {
      case 'clarify':
        return <ClarifyQueue items={data.stages.clarify} />
      case 'send_resume':
        return <SendResumeQueue items={data.stages.sendResume} />
      case 'wait_schedule':
        return <WaitQueue items={data.stages.waitSchedule} />
      case 'decide_apply':
        return <DecideApplyStage data={data.stages.decideApply} />
      default:
        return null
    }
  }, [active, data])

  return (
    <div className="app">
      <header className="top">
        <div>
          <h1>Pending actions</h1>
          <p className="subtitle">
            One path for every lead: Clarify → Send résumé → Wait / schedule → Decide / apply.
            Source is a badge, not a separate queue.
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
        <>
          <StagePipeline stages={data.pipeline} active={active} onSelect={setActive} />
          <main className="stage-panel">
            <h2>{data.pipeline.find((s) => s.id === active)?.label}</h2>
            <p className="panel-action">{data.pipeline.find((s) => s.id === active)?.action}</p>
            {stageBody}
          </main>
        </>
      )}

      {!data && !error && <p className="loading">Loading workflow…</p>}
    </div>
  )
}
