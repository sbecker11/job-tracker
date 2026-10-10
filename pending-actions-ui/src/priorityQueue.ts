import { leadAnchorId } from './lib/links'
import type {
  ArchivedLead,
  Channel,
  ClarifyItem,
  DecideLead,
  PipelineStageId,
  SendResumeItem,
  WaitItem,
  WorkflowPayload,
} from './types'

/** Contact-work stages — outbound reply / send / wait. */
export const CONTACT_STAGE_IDS: PipelineStageId[] = [
  'clarify',
  'send_resume',
  'wait_schedule',
]

/** Stages that still need a manual task (not closed/archived lookup). */
export const ACTIVE_STAGE_IDS: ReadonlySet<string> = new Set([
  'clarify',
  'send_resume',
  'wait_schedule',
  'decide_apply',
])

/**
 * Filter chips on the single priority list. These used to swap entire
 * panels (tabs). They now only narrow the same ranked list.
 */
export type ContactFilter = 'all' | PipelineStageId | 'duplicates_skipped' | 'archived'

export type QueueStage = PipelineStageId | 'duplicates_skipped' | 'archived'

export type StatusFilter = 'active' | 'any' | string

/** How the visible list is ordered. Default keeps task-urgency ranking. */
export type LeadSort = 'priority' | 'age_desc' | 'age_asc'

export type DecideBucket =
  | 'readyToApply'
  | 'needsDecision'
  | 'needsDecisionForced'
  | 'awaitingLlmReview'
  | 'jdUnresolved'

export interface ContactPriorityItem {
  id: string
  stage: QueueStage
  channel?: Channel
  company: string
  title: string
  recruiterName: string
  recruiterEmail?: string
  recruiterPhone?: string
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
  /** Full inbound message text — clarify rows only (see ClarifyItem). */
  messageBody?: string
  /** job_leads.status when this row is a tracked lead. */
  status?: string
  verdict?: string
  matchPct?: number
  commCount?: number
  decidedAt?: string
  duplicateOfKey?: string
  duplicateOfCompany?: string
  duplicateOfTitle?: string
  decideBucket?: DecideBucket
  lastComms?: Array<{ at?: string; direction?: string; summary?: string; body?: string }>
  initiation?: string
  replyVia?: string
  draftFitOk?: boolean
  draftFitNote?: string
  missedMeetingNote?: string
}

const STAGE_RANK: Record<string, number> = {
  clarify: 0,
  send_resume: 1,
  wait_schedule: 2,
  decide_apply: 3,
  duplicates_skipped: 8,
  archived: 9,
}

export function isActiveWork(item: ContactPriorityItem): boolean {
  return ACTIVE_STAGE_IDS.has(item.stage)
}

function urgency(item: ContactPriorityItem): number {
  if (item.replyDue) return 0
  if (item.followUpDue) return 1
  if (item.stage === 'decide_apply' && item.resumeRequested) return 2
  if (item.stage === 'clarify' || item.stage === 'send_resume' || item.stage === 'wait_schedule') {
    return 3
  }
  if (item.stage === 'decide_apply') return 4
  return 5
}

function recencyScore(item: ContactPriorityItem): number {
  if (item.stage === 'archived' || item.stage === 'duplicates_skipped') {
    const t = item.decidedAt ? Date.parse(item.decidedAt) : NaN
    return Number.isNaN(t) ? 0 : t
  }
  if (item.unansweredDays != null) return item.unansweredDays
  if (item.waitingDays != null) return item.waitingDays
  return item.ageDays
}

function sortKey(item: ContactPriorityItem): [number, number, number, number, string] {
  return [
    urgency(item),
    -item.contactAttempts,
    -recencyScore(item),
    STAGE_RANK[item.stage] ?? 9,
    (item.company || item.recruiterName || '').toLowerCase(),
  ]
}

function compareItems(a: ContactPriorityItem, b: ContactPriorityItem): number {
  const ka = sortKey(a)
  const kb = sortKey(b)
  for (let i = 0; i < ka.length; i++) {
    if (ka[i] < kb[i]) return -1
    if (ka[i] > kb[i]) return 1
  }
  return 0
}

/** Ranked list of every lead in the payload — active work first, then closed. */
export function buildContactPriorityQueue(data: WorkflowPayload): ContactPriorityItem[] {
  const byKey = new Map<string, ContactPriorityItem>()
  const noKey: ContactPriorityItem[] = []

  const take = (item: ContactPriorityItem) => {
    const key = item.normalizedKey || ''
    if (!key) {
      noKey.push(item)
      return
    }
    const prev = byKey.get(key)
    if (!prev || (STAGE_RANK[item.stage] ?? 9) < (STAGE_RANK[prev.stage] ?? 9)) {
      byKey.set(key, item)
    }
  }

  for (const c of data.stages.clarify) take(fromClarify(c))
  for (const s of data.stages.sendResume) take(fromSendResume(s))
  for (const w of data.stages.waitSchedule) take(fromWait(w))

  const da = data.stages.decideApply
  const decideBuckets: [DecideBucket, DecideLead[]][] = [
    ['readyToApply', da.readyToApply],
    ['needsDecision', da.needsDecision],
    ['needsDecisionForced', da.needsDecisionForced],
    ['awaitingLlmReview', da.awaitingLlmReview],
    ['jdUnresolved', da.jdUnresolved],
  ]
  for (const [bucket, rows] of decideBuckets) {
    for (const lead of rows) take(fromDecide(lead, bucket))
  }

  for (const lead of data.archivedLeads ?? []) take(fromArchived(lead))

  const merged = [...byKey.values(), ...collapseUnmatchedByThread(noKey)]
  merged.sort(compareItems)
  return merged
}

/** Unmatched InMails have no normalizedKey; IMAP + Gmail copies share threadUrl. */
function collapseUnmatchedByThread(items: ContactPriorityItem[]): ContactPriorityItem[] {
  const byThread = new Map<string, ContactPriorityItem>()
  const rest: ContactPriorityItem[] = []
  for (const item of items) {
    const thread = (item.threadUrl || '').trim()
    if (!thread) {
      rest.push(item)
      continue
    }
    const prev = byThread.get(thread)
    if (!prev) {
      byThread.set(thread, item)
      continue
    }
    byThread.set(thread, preferLinkedInCopy(prev, item))
  }
  return [...byThread.values(), ...rest]
}

function preferLinkedInCopy(
  a: ContactPriorityItem,
  b: ContactPriorityItem,
): ContactPriorityItem {
  const aImap = (a.messageId || '').toLowerCase().startsWith('imap:')
  const bImap = (b.messageId || '').toLowerCase().startsWith('imap:')
  if (aImap && !bImap) return b
  if (!aImap && bImap) return a
  return a
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
    recruiterPhone: c.recruiterPhone || '',
    emailIsLinkedInRelay: c.emailIsLinkedInRelay,
    ageDays: c.ageDays || 0,
    contactAttempts: c.contactAttempts || 1,
    draftReply: c.draftReply,
    messageBody: c.messageBody,
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
    status: c.status,
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
    recruiterPhone: s.recruiterPhone || '',
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
    status: s.status,
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
    recruiterPhone: w.recruiterPhone || '',
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
    status: w.status,
  }
}

function fromDecide(lead: DecideLead, bucket: DecideBucket): ContactPriorityItem {
  return {
    id: `decide:${bucket}:${lead.normalizedKey}`,
    stage: 'decide_apply',
    company: lead.company || '',
    title: lead.title || '',
    recruiterName: lead.recruiterName || '',
    recruiterEmail: lead.recruiterEmail || '',
    recruiterPhone: lead.recruiterPhone || '',
    ageDays: lead.ageDays || 0,
    contactAttempts: 0,
    normalizedKey: lead.normalizedKey,
    folderPath: lead.folderPath,
    companyFolderPath: lead.companyFolderPath,
    applyUrl: lead.applyUrl,
    resumeRequested: lead.resumeRequested,
    actionHint: lead.actionHint,
    nextAction: lead.nextAction || lead.actionHint,
    kind: bucket,
    duplicateCount: lead.duplicateCount,
    duplicateKeys: lead.duplicateKeys,
    status: lead.status,
    verdict: lead.verdict,
    matchPct: lead.matchPct,
    decideBucket: bucket,
  }
}

function fromArchived(lead: ArchivedLead): ContactPriorityItem {
  const isDup = Boolean(lead.duplicateOfKey)
  return {
    id: `archived:${lead.normalizedKey}`,
    stage: isDup ? 'duplicates_skipped' : 'archived',
    company: lead.company || '',
    title: lead.title || '',
    recruiterName: lead.recruiterName || '',
    recruiterEmail: lead.recruiterEmail || '',
    recruiterPhone: lead.recruiterPhone || '',
    ageDays: lead.ageDays || 0,
    contactAttempts: 0,
    normalizedKey: lead.normalizedKey,
    folderPath: lead.folderPath,
    companyFolderPath: lead.companyFolderPath,
    actionHint: lead.status
      ? `Status: ${lead.status === 'skipped' ? 'Closed - skipped' : lead.status}`
      : undefined,
    nextAction: isDup
      ? `Duplicate of ${lead.duplicateOfTitle || 'another lead'}${lead.duplicateOfCompany ? ` @ ${lead.duplicateOfCompany}` : ''}.`
      : 'Closed — browse history or update status if this should still be active.',
    kind: isDup ? 'duplicate' : 'archived',
    duplicateCount: lead.duplicateCount,
    duplicateKeys: lead.duplicateKeys,
    status: lead.status,
    verdict: lead.verdict,
    matchPct: lead.matchPct,
    commCount: lead.commCount,
    decidedAt: lead.decidedAt,
    duplicateOfKey: lead.duplicateOfKey,
    duplicateOfCompany: lead.duplicateOfCompany,
    duplicateOfTitle: lead.duplicateOfTitle,
  }
}

/** Which filter chip currently shows `normalizedKey`, if any. */
export function locateLeadTab(
  data: WorkflowPayload,
  normalizedKey: string,
): ContactFilter | null {
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
  if (inDecideApply) return 'all'
  const archived = (data.archivedLeads ?? []).find((l) => l.normalizedKey === normalizedKey)
  if (archived?.duplicateOfKey) return 'duplicates_skipped'
  if (archived) return 'archived'
  return null
}

/** Stage + status filters that keep `normalizedKey` visible on the single list. */
export function locateLeadFilters(
  data: WorkflowPayload,
  normalizedKey: string,
): { stage: ContactFilter; status: StatusFilter } | null {
  const tab = locateLeadTab(data, normalizedKey)
  if (!tab) return null
  if (tab === 'archived' || tab === 'duplicates_skipped') {
    const lead = (data.archivedLeads ?? []).find((l) => l.normalizedKey === normalizedKey)
    return { stage: tab, status: lead?.status || 'any' }
  }
  return { stage: 'all', status: 'active' }
}

/** Total row count across every decide/apply bucket. */
export function decideApplyCount(data: {
  readyToApply: unknown[]
  needsDecision: unknown[]
  needsDecisionForced: unknown[]
  awaitingLlmReview: unknown[]
  jdUnresolved: unknown[]
}): number {
  return (
    data.readyToApply.length +
    data.needsDecision.length +
    data.needsDecisionForced.length +
    data.awaitingLlmReview.length +
    data.jdUnresolved.length
  )
}

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

export function findKeyForAnchorId(data: WorkflowPayload, anchorId: string): string | null {
  for (const key of allLeadKeys(data)) {
    if (leadAnchorId(key) === anchorId) return key
  }
  return null
}

export function filterPriorityQueue(
  items: ContactPriorityItem[],
  stage: ContactFilter,
  status: StatusFilter,
  duplicateOfKey?: string | null,
): ContactPriorityItem[] {
  return items.filter((item) => {
    if (duplicateOfKey) {
      return item.duplicateOfKey === duplicateOfKey
    }
    if (stage === 'duplicates_skipped') {
      if (!item.duplicateOfKey) return false
    } else if (stage === 'archived') {
      if (item.stage !== 'archived' && item.stage !== 'duplicates_skipped') return false
    } else if (stage !== 'all' && item.stage !== stage) {
      return false
    }
    if (status === 'active') return isActiveWork(item)
    if (status === 'any' || !status) return true
    return (item.status || '') === status
  })
}

export function sortPriorityQueue(
  items: ContactPriorityItem[],
  sort: LeadSort,
): ContactPriorityItem[] {
  if (sort === 'priority') return items
  const copy = [...items]
  copy.sort((a, b) => {
    const diff = a.ageDays - b.ageDays
    if (diff !== 0) return sort === 'age_asc' ? diff : -diff
    return (a.company || a.recruiterName || '').localeCompare(
      b.company || b.recruiterName || '',
      undefined,
      { sensitivity: 'base' },
    )
  })
  return copy
}

/** @deprecated Prefer filterPriorityQueue — kept so older call sites still type-check. */
export function filterContactQueue(
  items: ContactPriorityItem[],
  filter: ContactFilter,
): ContactPriorityItem[] {
  return filterPriorityQueue(items, filter, filter === 'archived' || filter === 'duplicates_skipped' ? 'any' : 'active')
}

export function contactQueueCounts(items: ContactPriorityItem[]): Record<string, number> {
  const counts: Record<string, number> = {
    all: 0,
    clarify: 0,
    send_resume: 0,
    wait_schedule: 0,
    decide_apply: 0,
    duplicates_skipped: 0,
    archived: 0,
  }
  for (const i of items) {
    if (isActiveWork(i)) counts.all += 1
    counts[i.stage] = (counts[i.stage] || 0) + 1
    if (i.stage === 'duplicates_skipped') counts.archived += 1
  }
  return counts
}

export function statusCounts(items: ContactPriorityItem[]): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const i of items) {
    const status = (i.status || '').trim()
    if (!status) continue
    counts[status] = (counts[status] || 0) + 1
  }
  return counts
}

/** Stable id for the inbound the row is answering — used to lock Reply sent. */
export function replyAckKey(item: ContactPriorityItem): string {
  return item.messageId || item.replyId || item.id
}

export interface GlobalSearchResult {
  normalizedKey: string
  company: string
  title: string
  recruiterName?: string
  recruiterEmail?: string
  recruiterPhone?: string
  status?: string
  tab: ContactFilter
}

export function buildGlobalSearchIndex(data: WorkflowPayload): GlobalSearchResult[] {
  const out: GlobalSearchResult[] = []
  const seen = new Set<string>()
  const push = (r: Omit<GlobalSearchResult, 'normalizedKey'> & { normalizedKey?: string }) => {
    if (!r.normalizedKey || seen.has(r.normalizedKey)) return
    seen.add(r.normalizedKey)
    out.push(r as GlobalSearchResult)
  }

  for (const c of data.stages.clarify) {
    push({
      normalizedKey: c.normalizedKey,
      company: c.company || '',
      title: c.title || c.subject || '',
      recruiterName: c.recruiterName,
      recruiterEmail: c.recruiterEmail,
      recruiterPhone: c.recruiterPhone,
      status: c.status,
      tab: 'clarify',
    })
  }
  for (const s of data.stages.sendResume) {
    push({
      normalizedKey: s.normalizedKey,
      company: s.company,
      title: s.title,
      recruiterName: s.recruiterName,
      recruiterEmail: s.recruiterEmail,
      recruiterPhone: s.recruiterPhone,
      status: s.status,
      tab: 'send_resume',
    })
  }
  for (const w of data.stages.waitSchedule) {
    push({
      normalizedKey: w.normalizedKey,
      company: w.company,
      title: w.title,
      recruiterName: w.recruiterName,
      recruiterEmail: w.recruiterEmail,
      recruiterPhone: w.recruiterPhone,
      status: w.status,
      tab: 'wait_schedule',
    })
  }
  const da = data.stages.decideApply
  for (const r of [
    ...da.readyToApply,
    ...da.needsDecision,
    ...da.needsDecisionForced,
    ...da.awaitingLlmReview,
    ...da.jdUnresolved,
  ]) {
    push({
      normalizedKey: r.normalizedKey,
      company: r.company,
      title: r.title,
      recruiterName: r.recruiterName,
      recruiterEmail: r.recruiterEmail,
      recruiterPhone: r.recruiterPhone,
      status: r.status || r.verdict,
      tab: 'decide_apply',
    })
  }
  for (const l of data.archivedLeads ?? []) {
    push({
      normalizedKey: l.normalizedKey,
      company: l.company,
      title: l.title,
      recruiterName: l.recruiterName,
      recruiterEmail: l.recruiterEmail,
      recruiterPhone: l.recruiterPhone,
      status: l.status,
      tab: l.duplicateOfKey ? 'duplicates_skipped' : 'archived',
    })
  }
  return out
}

export function searchGlobalIndex(
  index: GlobalSearchResult[],
  query: string,
  limit = 20,
): GlobalSearchResult[] {
  const q = query.trim().toLowerCase()
  if (!q) return []
  const out: GlobalSearchResult[] = []
  for (const r of index) {
    const hit =
      r.company.toLowerCase().includes(q) ||
      r.title.toLowerCase().includes(q) ||
      (r.recruiterName || '').toLowerCase().includes(q) ||
      (r.recruiterEmail || '').toLowerCase().includes(q) ||
      (r.recruiterPhone || '').toLowerCase().includes(q)
    if (hit) {
      out.push(r)
      if (out.length >= limit) break
    }
  }
  return out
}
