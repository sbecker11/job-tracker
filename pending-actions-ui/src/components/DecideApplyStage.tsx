import { DuplicateBadge } from './DuplicateBadge'
import { leadAnchorId } from '../lib/links'
import type { DecideLead, WorkflowPayload } from '../types'

function revealFolderUrl(folderPath: string | undefined): string | null {
  if (!folderPath) return null
  return `revealfolder://reveal?path=${encodeURIComponent(folderPath)}`
}

/** Export + open this lead's job_conversations ODT (tools/view-communications/). */
function viewCommunicationsUrl(company?: string, title?: string): string | null {
  if (!company?.trim() || !title?.trim()) return null
  return `viewcomms://open?company=${encodeURIComponent(company)}&title=${encodeURIComponent(title)}`
}

function LeadTable({
  title,
  rows,
  showApply,
  onViewDuplicates,
  highlightKey,
}: {
  title: string
  rows: DecideLead[]
  showApply?: boolean
  onViewDuplicates?: (normalizedKey: string, firstDuplicateKey?: string) => void
  highlightKey?: string | null
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
              <tr
                key={r.normalizedKey}
                id={leadAnchorId(r.normalizedKey)}
                data-lead-key={r.normalizedKey}
                className={r.normalizedKey === highlightKey ? 'lead-highlight' : undefined}
              >
                <td>
                  {r.companyFolderPath ? (
                    <a
                      className="company-link"
                      href={revealFolderUrl(r.companyFolderPath) || '#'}
                      title="Open company folder in Finder"
                    >
                      {r.company}
                    </a>
                  ) : (
                    r.company
                  )}
                  {r.resumeRequested && (
                    <div className="resume-ask-flag">Recruiter asked for résumé</div>
                  )}
                </td>
                <td>
                  {r.folderPath ? (
                    <a
                      className="title-link"
                      href={revealFolderUrl(r.folderPath) || '#'}
                      title="Open this role's folder in Finder"
                    >
                      {r.title}
                    </a>
                  ) : (
                    r.title
                  )}
                  <DuplicateBadge
                    count={r.duplicateCount}
                    firstDuplicateKey={r.duplicateKeys?.[0]}
                    onView={
                      onViewDuplicates
                        ? () => onViewDuplicates(r.normalizedKey, r.duplicateKeys?.[0])
                        : undefined
                    }
                  />
                </td>
                <td className="num">{r.matchPct ?? '—'}</td>
                <td className="num">{r.ageDays}d</td>
                <td className="action-cell">
                  <div className="decide-actions">
                    <span>{r.nextAction || r.actionHint || 'Review reviews → pursue or skip'}</span>
                    {viewCommunicationsUrl(r.company, r.title) && (
                      <a
                        className="btn"
                        href={viewCommunicationsUrl(r.company, r.title)!}
                        title="Export and open full communications ODT for this lead"
                      >
                        History
                      </a>
                    )}
                  </div>
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
  onViewDuplicates,
  highlightKey,
}: {
  data: WorkflowPayload['stages']['decideApply']
  onViewDuplicates?: (normalizedKey: string, firstDuplicateKey?: string) => void
  highlightKey?: string | null
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
      <LeadTable
        title="Ready to apply"
        rows={data.readyToApply}
        showApply
        onViewDuplicates={onViewDuplicates}
        highlightKey={highlightKey}
      />
      <LeadTable
        title="Needs your decision"
        rows={data.needsDecision}
        onViewDuplicates={onViewDuplicates}
        highlightKey={highlightKey}
      />
      <LeadTable
        title="Needs decision (forced package)"
        rows={data.needsDecisionForced}
        onViewDuplicates={onViewDuplicates}
        highlightKey={highlightKey}
      />
      <LeadTable
        title="Awaiting full-LLM-review"
        rows={data.awaitingLlmReview}
        onViewDuplicates={onViewDuplicates}
        highlightKey={highlightKey}
      />
      <LeadTable
        title="JD unresolved"
        rows={data.jdUnresolved}
        onViewDuplicates={onViewDuplicates}
        highlightKey={highlightKey}
      />
    </div>
  )
}
