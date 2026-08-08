import type { DecideLead, WorkflowPayload } from '../types'

function LeadTable({
  title,
  rows,
  showApply,
}: {
  title: string
  rows: DecideLead[]
  showApply?: boolean
}) {
  if (!rows.length) return null
  return (
    <section className="subtable">
      <h3>
        {title} <span className="pill">{rows.length}</span>
      </h3>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Company</th>
              <th>Title</th>
              <th className="num">Match %</th>
              <th className="num">Age</th>
              {showApply && <th>Apply</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.normalizedKey}>
                <td>{r.company}</td>
                <td>{r.title}</td>
                <td className="num">{r.matchPct ?? '—'}</td>
                <td className="num">{r.ageDays}d</td>
                {showApply && (
                  <td>
                    {r.applyUrl ? (
                      <a href={r.applyUrl} target="_blank" rel="noreferrer">
                        Apply
                      </a>
                    ) : (
                      <span className="muted">No link</span>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

export function DecideApplyStage({
  data,
}: {
  data: WorkflowPayload['stages']['decideApply']
}) {
  const total =
    data.readyToApply.length +
    data.needsDecision.length +
    data.needsDecisionForced.length +
    data.awaitingLlmReview.length +
    data.jdUnresolved.length

  if (!total) {
    return <p className="empty-hint">No decide/apply work in the funnel right now.</p>
  }

  return (
    <div className="decide-stack">
      <LeadTable title="Ready to apply" rows={data.readyToApply} showApply />
      <LeadTable title="Needs your decision" rows={data.needsDecision} />
      <LeadTable title="Needs decision (forced package)" rows={data.needsDecisionForced} />
      <LeadTable title="Awaiting full-LLM-review" rows={data.awaitingLlmReview} />
      <LeadTable title="JD unresolved" rows={data.jdUnresolved} />
    </div>
  )
}
