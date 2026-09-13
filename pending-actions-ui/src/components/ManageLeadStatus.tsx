import { useState } from 'react'

/** Mirrors job_tracker.pipeline.models.LEAD_STAGES — kept as a plain literal
 * list here (not fetched from the backend) since the set of valid statuses
 * changes rarely and this avoids a round-trip just to populate a dropdown. */
export const LEAD_STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: 'new', label: 'New' },
  { value: 'pursued', label: 'Pursued' },
  { value: 'package_generated', label: 'Package generated' },
  { value: 'applied', label: 'Applied' },
  { value: 'following_up', label: 'Following up' },
  { value: 'interviewing', label: 'Interviewing' },
  { value: 'offered', label: 'Offered' },
  { value: 'accepted', label: 'Accepted' },
  { value: 'started', label: 'Started' },
  { value: 'skipped', label: 'Skipped' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'deleted', label: 'Deleted' },
  { value: 'unavailable', label: 'Unavailable' },
  { value: 'hired', label: 'Hired' },
]

function leadStatusUrl(key: string, status: string, reason: string): string {
  const params = new URLSearchParams()
  params.set('key', key)
  params.set('status', status)
  if (reason.trim()) params.set('reason', reason.trim())
  return `leadstatus://set?${params.toString()}`
}

interface Props {
  normalizedKey?: string
  /** Preselects the dropdown when the caller already knows the lead's
   * current status (e.g. Archived/Duplicates-skipped rows) — purely a UX
   * nicety, not required for the action itself. */
  currentStatus?: string
}

/** "Manage lead" — a self-service status-change control, added 2026-08-26
 * after a Thyme Care lead sat marked "skipped" despite Shawn having already
 * applied, and the only way to fix it was a hand-run SQL update. Shells out
 * (via the `leadstatus://` URL scheme, tools/set-lead-status/) to
 * `set-lead-status`, which both flips job_leads.status and appends the
 * reason to job_leads.notes — see store.append_lead_note(). Deliberately
 * one small reusable control rendered on every tab's rows (Clarify, Send
 * résumé, Wait/schedule, Decide/apply, Duplicates skipped, Archived)
 * instead of a separate full lead-editing panel, which is a much larger
 * scope that was discussed before but never actually shipped. */
export function ManageLeadStatus({ normalizedKey, currentStatus }: Props) {
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState(currentStatus || 'applied')
  const [reason, setReason] = useState('')
  const [sent, setSent] = useState(false)

  if (!normalizedKey) return null

  return (
    <div className="manage-lead">
      <button
        type="button"
        className="btn link muted"
        onClick={() => setOpen((o) => !o)}
      >
        {open ? 'Manage lead ▲' : 'Manage lead ▾'}
      </button>
      {open && (
        <div className="manage-lead-form">
          <label className="manage-lead-field">
            <span>Status</span>
            <select
              value={status}
              onChange={(e) => {
                setStatus(e.target.value)
                setSent(false)
              }}
            >
              {LEAD_STATUS_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </label>
          <input
            className="manage-lead-reason"
            type="text"
            placeholder="Reason (optional — logged as a note)"
            value={reason}
            onChange={(e) => {
              setReason(e.target.value)
              setSent(false)
            }}
          />
          <a
            className={`btn${sent ? ' muted' : ''}`}
            href={leadStatusUrl(normalizedKey, status, reason)}
            onClick={() => setSent(true)}
          >
            {sent ? 'Applied ✓ — Regenerate to see it' : 'Apply'}
          </a>
        </div>
      )}
    </div>
  )
}
