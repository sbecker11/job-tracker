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

export type ContactFilter = 'all' | PipelineStageId

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
  }
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
