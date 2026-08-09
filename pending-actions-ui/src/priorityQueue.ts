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
  ageDays: number
  contactAttempts: number
  waitingDays?: number
  draftReply?: string
  threadUrl?: string
  messageId?: string
  normalizedKey?: string
  replyId?: string
  folderPath?: string
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

function sortKey(item: ContactPriorityItem): [number, number, number, string] {
  // Attempts ↓, age ↓, then stage urgency (clarify before wait), then name.
  return [
    -item.contactAttempts,
    -item.ageDays,
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
    ageDays: c.ageDays || 0,
    contactAttempts: c.contactAttempts || 1,
    draftReply: c.draftReply,
    threadUrl: c.threadUrl,
    messageId: c.messageId,
    normalizedKey: c.normalizedKey,
    replyId: c.replyId,
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
    ageDays: s.ageDays || 0,
    contactAttempts: s.contactAttempts || 1,
    normalizedKey: s.normalizedKey,
    folderPath: s.folderPath,
    applyUrl: s.applyUrl,
    threadUrl: s.threadUrl,
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
    recruiterName: '',
    ageDays: w.ageDays || 0,
    contactAttempts: w.contactAttempts || 1,
    waitingDays: w.waitingDays,
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
