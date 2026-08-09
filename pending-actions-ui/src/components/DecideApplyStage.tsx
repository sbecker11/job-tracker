import type { DecideLead, WorkflowPayload } from '../types'

function revealFolderUrl(folderPath: string): string | null {
  if (!folderPath) return null
  // Decide leads often carry relative paths; reveal helper needs absolute.
  // Absolute paths already work; relative ones are opened via Regenerate HTML root elsewhere.
  if (folderPath.startsWith('/')) {
    return `revealfolder://reveal?path=${encodeURIComponent(folderPath)}`
  }
  return `revealfolder://reveal?path=${encodeURIComponent(
    `${'/Users/sbecker11/Desktop/Resumes/2026'}/${folderPath}`.replace(/\/+/g, '/'),
  )}`
}

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
              <th>YOUR ACTION</th>
              {showApply && <th>Apply</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.normalizedKey}>
                <td>
                  {r.company}
                  {r.resumeRequested && (
                    <div className="resume-ask-flag">Recruiter asked for résumé</div>
                  )}
                </td>
                <td>
                  {r.folderPath ? (
                    <a href={revealFolderUrl(r.folderPath) || '#'}>{r.title}</a>
                  ) : (
                    r.title
                  )}
                </td>
                <td className="num">{r.matchPct ?? '—'}</td>
                <td className="num">{r.ageDays}d</td>
                <td className="action-cell">
                  {r.nextAction || r.actionHint || 'Review reviews → pursue or skip'}
                </td>
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
      <p className="hint-line">
        Decide/apply is where you read the reviews and choose pursue (generate package) or skip.
        Contact priority Send résumé only appears after the package exists on disk.
      </p>
      <LeadTable title="Ready to apply" rows={data.readyToApply} showApply />
      <LeadTable title="Needs your decision" rows={data.needsDecision} />
      <LeadTable title="Needs decision (forced package)" rows={data.needsDecisionForced} />
      <LeadTable title="Awaiting full-LLM-review" rows={data.awaitingLlmReview} />
      <LeadTable title="JD unresolved" rows={data.jdUnresolved} />
    </div>
  )
}
