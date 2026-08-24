import { leadAnchorId } from './lib/links'
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

/** 'duplicates_skipped' and 'archived' are top-level tabs like the others,
 * but their content (ArchivedLead rows) comes from data.archivedLeads, not
 * the clarify/send_resume/wait_schedule/decide_apply funnel — App.tsx
 * special-cases them the same way it already special-cases 'decide_apply'.
 *
 * 2026-08-18: 'archived' was previously a collapsible <details> below the
 * tabs, not a tab itself, reachable only via an `archiveOpen` boolean. That
 * turned out to be exactly the same mistake 'duplicates_skipped' made
 * before its own 2026-08-17 promotion (see DuplicatesSkippedPanel's
 * comment): a survivor jump landing there had to open the <details>,
 * re-render 1200+ archived rows, and then scroll a real (sometimes huge)
 * distance down the page to reach it — all while racing a fixed-duration
 * highlight flash. Confirmed live: the jump would set the highlight
 * correctly but the scroll itself would time out before that re-render
 * settled, landing on nothing visible. Making 'archived' a proper tab
 * means jumping there is just `setFilter('archived')` — same panel
 * position as every other tab, no <details>, no separately-triggered
 * re-render to wait out. */
export type ContactFilter = 'all' | PipelineStageId | 'duplicates_skipped' | 'archived'

export interface ContactPriorityItem {
  id: string
  stage: 'clarify' | 'send_resume' | 'wait_schedule'
  channel: Channel
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
  directRecruiterOutreach?: boolean | null
  matchPct?: number
  markSentUrl?: string
  actionHint?: string
  nextAction?: string
  kind: string
  duplicateCount?: number
  duplicateKeys?: string[]
  /** Full inbound message text — clarify rows only (see ClarifyItem). */
  messageBody?: string
}

const STAGE_RANK: Record<string, number> = {
  clarify: 0,
  send_resume: 1,
  wait_schedule: 2,
}

function droRank(item: ContactPriorityItem): number {
  const dro = item.directRecruiterOutreach
  if (dro === true) return 0
  if (dro === false) return 2
  return 1
}

function sortKey(item: ContactPriorityItem): [number, number, number, number, number, number, string] {
  // Mirror pending_workflow._priority_sort_key: reply-due → direct recruiter → attempts → match → age.
  const urgency = item.replyDue ? 0 : item.followUpDue ? 1 : 2
  const recency =
    item.unansweredDays != null
      ? item.unansweredDays
      : item.waitingDays != null
        ? item.waitingDays
        : item.ageDays
  const match = -(item.matchPct ?? 0)
  return [
    urgency,
    droRank(item),
    -item.contactAttempts,
    match,
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
    directRecruiterOutreach: s.directRecruiterOutreach,
    matchPct: s.matchPct,
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
  }
}

/** Which top-level tab currently shows `normalizedKey`, if any — used to
 * jump from a duplicate's "Duplicate of X" link (on the Duplicates skipped
 * tab) back to the survivor lead wherever it actually lives. Returns null
 * when the survivor isn't tracked in the payload at all (e.g. removed from
 * the database) — there's nowhere to jump in that case.
 *
 * 'archived' means the lead lives in data.archivedLeads — a real tab as of
 * 2026-08-18 (see ContactFilter's comment), not a separately-opened
 * <details> below the tabs. Added 2026-08-17 after "Go to this lead"
 * reported leads as unreachable when a survivor had itself since been
 * fully decided (e.g. applied → hired/rejected) and moved out of every
 * active-funnel bucket. */
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
  if (inDecideApply) return 'decide_apply'
  const inArchived = (data.archivedLeads ?? []).some((l) => l.normalizedKey === normalizedKey)
  if (inArchived) return 'archived'
  return null
}

/** Total row count across every decide/apply bucket — lives here (not in
 * ContactFilterBar.tsx, its only caller) purely so that component file only
 * exports components, keeping React Fast Refresh happy. */
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

/** Reverse-lookup for a URL fragment produced by `leadAnchorId` — used to
 * restore a lead's location (which tab, or the Archived panel) when the
 * page is loaded/reloaded/opened in a new tab with a `#lead-...` hash
 * already in the address bar, e.g. a pasted or bookmarked link that a
 * previous "Duplicate of" / "Go to this lead" click produced (App.tsx's
 * onJumpToSurvivor/onViewDuplicates only ever wired up the *in-app click*
 * case — a fresh navigation straight to that URL had no equivalent
 * restoration logic, which is the actual explanation behind "the
 * duplicate-of link's address does nothing" reports that turned out to be
 * about pasting/reloading that URL rather than clicking inside an
 * already-mounted page). `leadAnchorId`'s character-collapsing isn't
 * cleanly invertible (multiple distinct separators all become `-`), so
 * this scans every real key actually present in `data` and matches by
 * recomputing each candidate's own anchor id, rather than trying to parse
 * the id back into "company::title". */
export function findKeyForAnchorId(data: WorkflowPayload, anchorId: string): string | null {
  for (const key of allLeadKeys(data)) {
    if (leadAnchorId(key) === anchorId) return key
  }
  return null
}

export function filterContactQueue(
  items: ContactPriorityItem[],
  filter: ContactFilter,
): ContactPriorityItem[] {
  // 'duplicates_skipped' / 'archived' render their own dedicated panel
  // (App.tsx) sourced from data.archivedLeads, not this contact-priority
  // list — this function's result goes unused for those two, but return
  // early rather than let `i.stage === filter` (never true for either)
  // silently produce a misleading empty array.
  if (filter === 'all' || filter === 'decide_apply' || filter === 'duplicates_skipped' || filter === 'archived') {
    return items
  }
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

/** One entry in the cross-tab "All tabs" search index (see AllTabsToggle) —
 * every lead anywhere in the payload, tagged with which tab actually shows
 * it, so a search typed into any one tab's box can surface a hit that
 * actually lives in a different tab and jump straight there. */
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

/** Builds the cross-tab index once per data refresh (App.tsx memoizes on
 * `data`) — deliberately keyed/deduped by normalizedKey, first occurrence
 * wins (contact stages checked before decide/apply before archived), so a
 * lead present in more than one bucket (shouldn't normally happen, but
 * cheaper to guard than assume) only shows once. Items with no
 * normalizedKey (e.g. an `unmatched` clarify row with nothing extracted
 * yet) are skipped — there's no anchor to jump to and nothing to key on. */
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
      status: r.verdict,
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
      tab: 'archived',
    })
  }
  return out
}

/** Same field set as every per-tab local search (company/title/recruiter
 * name/email/phone) — capped at `limit` matches so a broad query (e.g. a
 * one-letter typo) can't render a dropdown of hundreds of rows. */
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
