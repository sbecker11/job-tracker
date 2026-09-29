import { useEffect } from 'react'

/** Mirrors what the header Regenerates button actually runs
 * (`refreshpending://run?no_open=1` → `render_pending_actions.py`
 * without `--no-rescore`). Kept as a static list so the help modal does
 * not need a live payload. Update this if the helper or renderer steps
 * change. */
const REGENERATE_STEPS: { title: string; detail: string }[] = [
  {
    title: 'Launch RefreshPending',
    detail:
      'Opens refreshpending://run?no_open=1. The helper runs this checkout’s Python renderer; it does not open a new browser tab.',
  },
  {
    title: 'Rescore new leads (rule-based)',
    detail:
      'Recomputes match_pct / verdict / matched_skills for every status=new lead that has JD text, using the current free scorer. Skips REVIEW NEEDED rows. No LLM call.',
  },
  {
    title: 'Heal applied CRM',
    detail:
      'Idempotent fix-up: stamp missing applied_at timestamps and skip same-URL twins of already-applied postings so they do not keep showing as Ready to apply.',
  },
  {
    title: 'Promote unmatched LinkedIn pitches',
    detail:
      'Turns structured InMail (Position + agency) parked as unmatched into stub leads. Heuristic only — no LLM.',
  },
  {
    title: 'Bucket leads and count packages',
    detail:
      'Reads leads.db, assigns each lead to Clarify / Send / Wait / Decide buckets, and counts résumé/cover files on disk.',
  },
  {
    title: 'Draft qualifying replies',
    detail:
      'Builds copy-ready qualifying-reply text for unmatched communications. Never sends mail.',
  },
  {
    title: 'Read schedule health',
    detail:
      'Reads recruiting-automation HALT / last-OK cycle / monthly uptime from sibling state and logs.',
  },
  {
    title: 'Write snapshots',
    detail:
      'Writes var/pending-actions.html, var/pending-actions.json, and pending-actions-ui/public/pending-actions.json.',
  },
  {
    title: 'Reload this tab',
    detail:
      'This page polls until generatedAt changes, then reloads the JSON. It does not triage Gmail/IMAP, run LLM scoring, generate packages, or resync labels.',
  },
]

export function RegenerateHelpModal({ onClose }: { onClose: () => void }) {
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
        aria-labelledby="regenerate-help-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h2 id="regenerate-help-title">What Regenerates does</h2>
          <button type="button" className="link-button modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <p className="hint-line">
          In order. This is a dashboard refresh, not a full hourly cycle — no inbox triage, no
          LLM scoring, no package generation, no send.
        </p>
        <ol className="cycle-steps-list">
          {REGENERATE_STEPS.map((step) => (
            <li key={step.title}>
              <div className="cycle-step-row">
                <span className="cycle-step-title">{step.title}</span>
              </div>
              <p className="cycle-step-detail">{step.detail}</p>
            </li>
          ))}
        </ol>
      </div>
    </div>
  )
}
