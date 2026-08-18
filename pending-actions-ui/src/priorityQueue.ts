import type {
  Channel,
  ClarifyItem,
  PipelineStageId,
  SendResumeItem,
  WaitItem,
  WorkflowPayload,
} from './types'

/** Contact-work stages only — Decide/apply is separate (not outbound contact). */
export const CONTACT_STAGE_IDS: PipelineStageId[] = [
  'clarify',
  'send_resume',
  'wait_schedule',
]

/** 'duplicates_skipped' is a top-level tab like the others, but its content
 * (ArchivedLead rows carrying duplicateOfKey) comes from data.archivedLeads,
 * not the clarify/send_resume/wait_schedule/decide_apply funnel — App.tsx
 * special-cases it the same way it already special-cases 'decide_apply'. */
export type ContactFilter = 'all' | PipelineStageId | 'duplicates_skipped'

export interface ContactPriorityItem {
  id: string
  stage: 'clarify' | 'send_resume' | 'wait_schedule'
  channel: Channel
  company: string
  title: string
  recruiterName: string
  recruiterEmail?: string
  emailIsLinkedInRelay?: boolean
  ageDays: number
  contactAttempts: number
  waitingDays?: number
  followUpDue?: boolean
  followUpThresholdDays?: number
  replyDue?: boolean
  unansweredDays?: number
  draftReply?: string
  threadUrl?: string
  gmailUrl?: string
  messageId?: string
  normalizedKey?: string
  replyId?: string
  folderPath?: string
  companyFolderPath?: string
  applyUrl?: string
  packageReady?: boolean
  resumeRequested?: boolean
  markSentUrl?: string
  actionHint?: string
  nextAction?: string
  kind: string
  duplicateCount?: number
  duplicateKeys?: string[]
}

const STAGE_RANK: Record<string, number> = {
  clarify: 0,
  send_resume: 1,
  wait_schedule: 2,
}

function sortKey(item: ContactPriorityItem): [number, number, number, number, string] {
  // Recruiter waiting on Shawn (replyDue) beats overdue Wait follow-ups.
  const urgency = item.replyDue ? 0 : item.followUpDue ? 1 : 2
  const recency =
    item.unansweredDays != null
      ? item.unansweredDays
      : item.waitingDays != null
        ? item.waitingDays
        : item.ageDays
  return [
    urgency,
    -item.contactAttempts,
    -recency,
    STAGE_RANK[item.stage] ?? 9,
    (item.company || item.recruiterName || '').toLowerCase(),
  ]
}

export function buildContactPriorityQueue(data: WorkflowPayload): ContactPriorityItem[] {
  const items: ContactPriorityItem[] = []

  for (const c of data.stages.clarify) {
    items.push(fromClarify(c))
  }
  for (const s of data.stages.sendResume) {
    items.push(fromSendResume(s))
  }
  for (const w of data.stages.waitSchedule) {
    items.push(fromWait(w))
  }

  // Dedupe by normalizedKey when the same lead appears in multiple buckets
  // (prefer clarify > send_resume > wait).
  const byKey = new Map<string, ContactPriorityItem>()
  const noKey: ContactPriorityItem[] = []
  for (const item of items) {
    const key = item.normalizedKey || ''
    if (!key) {
      noKey.push(item)
      continue
    }
    const prev = byKey.get(key)
    if (!prev || (STAGE_RANK[item.stage] ?? 9) < (STAGE_RANK[prev.stage] ?? 9)) {
      byKey.set(key, item)
    }
  }

  const merged = [...byKey.values(), ...noKey]
  merged.sort((a, b) => {
    const ka = sortKey(a)
    const kb = sortKey(b)
    for (let i = 0; i < ka.length; i++) {
      if (ka[i] < kb[i]) return -1
      if (ka[i] > kb[i]) return 1
    }
    return 0
  })
  return merged
}

function fromClarify(c: ClarifyItem): ContactPriorityItem {
  return {
    id: c.replyId || c.messageId || c.normalizedKey || `clarify:${c.subject}:${c.recruiterName}`,
    stage: 'clarify',
    channel: c.channel,
    company: c.company || '',
    title: c.title || c.subject || '',
    recruiterName: c.recruiterName || '',
    recruiterEmail: c.recruiterEmail || '',
    emailIsLinkedInRelay: c.emailIsLinkedInRelay,
    ageDays: c.ageDays || 0,
    contactAttempts: c.contactAttempts || 1,
    draftReply: c.draftReply,
    threadUrl: c.threadUrl,
    gmailUrl: c.gmailUrl,
    messageId: c.messageId,
    normalizedKey: c.normalizedKey,
    replyId: c.replyId,
    replyDue: c.replyDue,
    unansweredDays: c.unansweredDays,
    folderPath: c.folderPath,
    companyFolderPath: c.companyFolderPath,
    actionHint: c.actionHint,
    nextAction: c.nextAction || c.actionHint,
    kind: c.kind,
    duplicateCount: c.duplicateCount,
    duplicateKeys: c.duplicateKeys,
  }
}

function fromSendResume(s: SendResumeItem): ContactPriorityItem {
  return {
    id: `send:${s.normalizedKey}`,
    stage: 'send_resume',
    channel: s.channel,
    company: s.company || '',
    title: s.title || '',
    recruiterName: s.recruiterName || '',
    recruiterEmail: s.recruiterEmail || '',
    emailIsLinkedInRelay: s.emailIsLinkedInRelay,
    ageDays: s.ageDays || 0,
    contactAttempts: s.contactAttempts || 1,
    normalizedKey: s.normalizedKey,
    folderPath: s.folderPath,
    companyFolderPath: s.companyFolderPath,
    applyUrl: s.applyUrl,
    threadUrl: s.threadUrl,
    gmailUrl: s.gmailUrl,
    packageReady: s.packageReady,
    resumeRequested: s.resumeRequested,
    draftReply: s.draftReply,
    markSentUrl: s.markSentUrl,
    actionHint: s.actionHint,
    nextAction: s.nextAction || s.actionHint,
    kind: s.kind,
    duplicateCount: s.duplicateCount,
    duplicateKeys: s.duplicateKeys,
  }
}

function fromWait(w: WaitItem): ContactPriorityItem {
  return {
    id: `wait:${w.normalizedKey}`,
    stage: 'wait_schedule',
    channel: w.channel,
    company: w.company || '',
    title: w.title || '',
    recruiterName: w.recruiterName || '',
    recruiterEmail: w.recruiterEmail || '',
    emailIsLinkedInRelay: w.emailIsLinkedInRelay,
    ageDays: w.ageDays || 0,
    contactAttempts: w.contactAttempts || 1,
    waitingDays: w.waitingDays,
    followUpDue: w.followUpDue,
    followUpThresholdDays: w.followUpThresholdDays,
    draftReply: w.draftReply,
    threadUrl: w.threadUrl,
    gmailUrl: w.gmailUrl,
    markSentUrl: w.markSentUrl,
    folderPath: w.folderPath,
    companyFolderPath: w.companyFolderPath,
    normalizedKey: w.normalizedKey,
    actionHint: w.actionHint,
    nextAction: w.nextAction || w.actionHint,
    kind: w.kind,
    duplicateCount: w.duplicateCount,
    duplicateKeys: w.duplicateKeys,
  }
}

/** Which top-level tab currently shows `normalizedKey`, if any — used to
 * jump from a duplicate's "Duplicate of X" link (on the Duplicates skipped
 * tab) back to the survivor lead wherever it actually lives. Returns null
 * when the survivor isn't tracked in the payload at all (e.g. removed from
 * the database) — there's nowhere to jump in that case.
 *
 * 'archived' means the lead lives in data.archivedLeads (the "Archived /
 * decided leads" panel below the tabs, not one of the ContactFilter tabs
 * itself) — distinct return value so App.tsx knows to open that panel
 * instead of switching filter tabs. Added 2026-08-17 after "Go to this
 * lead" reported leads as unreachable when a survivor had itself since
 * been fully decided (e.g. applied → hired/rejected) and moved out of every
 * active-funnel bucket. */
export function locateLeadTab(
  data: WorkflowPayload,
  normalizedKey: string,
): ContactFilter | 'archived' | null {
  const inContactStage = [...data.stages.clarify, ...data.stages.sendResume, ...data.stages.waitSchedule].some(
    (i) => i.normalizedKey === normalizedKey,
  )
  if (inContactStage) return 'all'
  const da = data.stages.decideApply
  const inDecideApply = [
    ...da.readyToApply,
    ...da.needsDecision,
    ...da.needsDecisionForced,
    ...da.awaitingLlmReview,
    ...da.jdUnresolved,
  ].some((i) => i.normalizedKey === normalizedKey)
  if (inDecideApply) return 'decide_apply'
  const inArchived = (data.archivedLeads ?? []).some((l) => l.normalizedKey === normalizedKey)
  if (inArchived) return 'archived'
  return null
}

/** Every normalizedKey present anywhere in `data` — contact-stage items,
 * every decide/apply bucket, and archivedLeads. Same source lists as
 * locateLeadTab above; used to diff before/after snapshots around an
 * on-demand single-message IMAP triage run (App.tsx's "Check inbox now"
 * button) so the UI can tell which lead, if any, is newly present and
 * jump straight to it. */
export function allLeadKeys(data: WorkflowPayload): Set<string> {
  const keys = new Set<string>()
  for (const i of [...data.stages.clarify, ...data.stages.sendResume, ...data.stages.waitSchedule]) {
    if (i.normalizedKey) keys.add(i.normalizedKey)
  }
  const da = data.stages.decideApply
  for (const i of [
    ...da.readyToApply,
    ...da.needsDecision,
    ...da.needsDecisionForced,
    ...da.awaitingLlmReview,
    ...da.jdUnresolved,
  ]) {
    keys.add(i.normalizedKey)
  }
  for (const l of data.archivedLeads ?? []) {
    keys.add(l.normalizedKey)
  }
  return keys
}

export function filterContactQueue(
  items: ContactPriorityItem[],
  filter: ContactFilter,
): ContactPriorityItem[] {
  if (filter === 'all' || filter === 'decide_apply') return items
  return items.filter((i) => i.stage === filter)
}

export function contactQueueCounts(items: ContactPriorityItem[]): Record<string, number> {
  const counts: Record<string, number> = {
    all: items.length,
    clarify: 0,
    send_resume: 0,
    wait_schedule: 0,
  }
  for (const i of items) {
    counts[i.stage] = (counts[i.stage] || 0) + 1
  }
  return counts
}

/** Stable id for the inbound the row is answering — used to lock Reply sent. */
export function replyAckKey(item: ContactPriorityItem): string {
  return item.messageId || item.replyId || item.id
}
