export type PipelineStageId = 'clarify' | 'send_resume' | 'wait_schedule' | 'decide_apply'

/** One `run_step` call's outcome from the latest recruiting-automation
 * cycle log — `title` is that step's exact log description string (see
 * recruiting-automation/run_cycle.sh), matched by prefix against the
 * static step list in ScheduleHealthBanner.tsx rather than duplicated
 * verbatim there, so cosmetic rewording in that sibling repo's script
 * doesn't silently break the match. */
export interface CycleStepTiming {
  title: string
  seconds: number | null
  status: 'ok' | 'failed' | 'timed_out' | 'incomplete'
}

export type Channel = 'linkedin' | 'email'

export interface PipelineStage {
  id: PipelineStageId
  label: string
  action: string
  count: number
}

export interface ClarifyItem {
  kind: string
  stage: string
  channel: Channel
  recruiterName?: string
  recruiterEmail?: string
  recruiterPhone?: string
  /** True when recruiterEmail is uuid@reply.linkedin.com (not a personal inbox). */
  emailIsLinkedInRelay?: boolean
  subject?: string
  company?: string
  title?: string
  threadUrl?: string
  /** Gmail deep-link pinned to shawnbecker.recruiting@gmail.com (authuser=). */
  gmailUrl?: string
  draftReply?: string
  /** Full inbound recruiter message this clarify row is answering — not just
   * the drafted reply. Added 2026-08-23: for `kind: "unmatched"` rows in
   * particular (no company/title extracted, so no JD/no-LLM/full-LLM review
   * docs exist yet either), this was previously the *only* place the actual
   * pitch text lived on disk (var/pending-actions.json's
   * unmatchedCommunications array), with no way to see it from this card at
   * all — surfaced via ContactPriorityQueue's "Show steps" detail. LinkedIn's
   * invisible tracking-pixel padding (U+034F runs) is already stripped
   * server-side (pending_workflow.clean_message_body_for_display). */
  messageBody?: string
  ageDays: number
  messageId?: string
  normalizedKey?: string
  replyId?: string
  contactAttempts: number
  replyDue?: boolean
  unansweredDays?: number
  actionHint?: string
  nextAction?: string
  folderPath?: string
  companyFolderPath?: string
  /** Count of, and normalizedKeys for, other leads marked as duplicates of
   * this one (store.mark_duplicate). Only present (and non-empty) when at
   * least one exists — see the "Duplicates skipped" tab in App.tsx. */
  duplicateCount?: number
  duplicateKeys?: string[]
}

export interface SendResumeItem {
  kind: string
  stage: string
  channel: Channel
  company: string
  title: string
  normalizedKey: string
  recruiterName?: string
  recruiterEmail?: string
  recruiterPhone?: string
  emailIsLinkedInRelay?: boolean
  ageDays: number
  matchPct?: number
  folderPath?: string
  companyFolderPath?: string
  applyUrl?: string
  threadUrl?: string
  gmailUrl?: string
  contactAttempts: number
  resumeRequested?: boolean
  packageReady?: boolean
  draftReply?: string
  markSentUrl?: string
  actionHint?: string
  nextAction?: string
  duplicateCount?: number
  duplicateKeys?: string[]
}

export interface WaitItem {
  kind: string
  stage: string
  channel: Channel
  company: string
  title: string
  normalizedKey: string
  recruiterName?: string
  recruiterEmail?: string
  recruiterPhone?: string
  emailIsLinkedInRelay?: boolean
  ageDays: number
  waitingDays: number
  awaitingSince?: string
  status?: string
  contactAttempts: number
  followUpDue?: boolean
  followUpThresholdDays?: number
  draftReply?: string
  threadUrl?: string
  gmailUrl?: string
  markSentUrl?: string
  folderPath?: string
  companyFolderPath?: string
  actionHint?: string
  nextAction?: string
  duplicateCount?: number
  duplicateKeys?: string[]
}

export interface DecideLead {
  company: string
  title: string
  normalizedKey: string
  ageDays: number
  matchPct?: number
  verdict?: string
  applyUrl?: string
  folderPath?: string
  companyFolderPath?: string
  stage?: string
  resumeRequested?: boolean
  nextAction?: string
  actionHint?: string
  duplicateCount?: number
  duplicateKeys?: string[]
  recruiterName?: string
  recruiterEmail?: string
  recruiterPhone?: string
}

/** A lead a decision has already been made on (skipped/rejected/deleted/
 * unavailable/hired) — off the active funnel, but still browsable for its
 * message history via commCount + View communications. */
export interface ArchivedLead {
  normalizedKey: string
  company: string
  title: string
  status: string
  matchPct?: number
  verdict?: string
  decidedAt?: string
  ageDays: number
  commCount: number
  folderPath?: string
  companyFolderPath?: string
  /** Set when this lead was skipped specifically because it's the same
   * real opportunity as another job_leads row (store.mark_duplicate) —
   * distinct from a plain skip (dealbreaker, JD mismatch, etc.). */
  duplicateOfKey?: string
  duplicateOfCompany?: string
  duplicateOfTitle?: string
  /** Count of, and normalizedKeys for, other leads marked as duplicates of
   * THIS lead (i.e. this lead is itself a survivor with its own duplicates,
   * distinct from duplicateOfKey above which is set when this lead IS a
   * duplicate of something else). */
  duplicateCount?: number
  duplicateKeys?: string[]
  /** Earliest-contacted recruiter on file for this lead (job_contacts),
   * surfaced (2026-08-19) so the search box can match on recruiter/
   * email/phone in addition to company/title. */
  recruiterName?: string
  recruiterEmail?: string
  recruiterPhone?: string
}

export interface WorkflowPayload {
  generatedAt: string
  folderRoot?: string
  waitFollowupDays?: number
  pipeline: PipelineStage[]
  stages: {
    clarify: ClarifyItem[]
    sendResume: SendResumeItem[]
    waitSchedule: WaitItem[]
    decideApply: {
      readyToApply: DecideLead[]
      needsDecision: DecideLead[]
      needsDecisionForced: DecideLead[]
      awaitingLlmReview: DecideLead[]
      jdUnresolved: DecideLead[]
    }
  }
  totalLeads: number
  notPrioritizedCount: number
  scheduleHealth?: {
    level?: string
    summary?: string
    /** ISO timestamp of the last successful cycle (`state/last_ok_cycle`),
     * parseable by `new Date()` — lets the UI tick a live "ago" counter
     * the same way it already does for `generatedAt` (see App.tsx's
     * formatGeneratedAgo), instead of just showing the server-rendered
     * `summary` string as of whenever this JSON was last regenerated. */
    lastOkAtIso?: string
    /** Per-step wall-clock time from the most recent recruiting-automation
     * cycle log (see render_pending_actions.py's
     * `_parse_latest_cycle_step_timings`) — one entry per `run_step` call
     * in that cycle's log, in the order they ran. May be empty if no log
     * was found yet. */
    cycleSteps?: CycleStepTiming[]
  }
  archivedLeads?: ArchivedLead[]
}

export const STAGE_ORDER: PipelineStageId[] = [
  'clarify',
  'send_resume',
  'wait_schedule',
  'decide_apply',
]
