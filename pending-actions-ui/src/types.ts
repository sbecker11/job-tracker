export type PipelineStageId = 'clarify' | 'send_resume' | 'wait_schedule' | 'decide_apply'

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
  subject?: string
  company?: string
  title?: string
  threadUrl?: string
  draftReply?: string
  ageDays: number
  messageId?: string
  normalizedKey?: string
  replyId?: string
  contactAttempts: number
  actionHint?: string
  nextAction?: string
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
  ageDays: number
  matchPct?: number
  folderPath?: string
  applyUrl?: string
  threadUrl?: string
  contactAttempts: number
  resumeRequested?: boolean
  packageReady?: boolean
  draftReply?: string
  markSentUrl?: string
  actionHint?: string
  nextAction?: string
}

export interface WaitItem {
  kind: string
  stage: string
  channel: Channel
  company: string
  title: string
  normalizedKey: string
  ageDays: number
  waitingDays: number
  awaitingSince?: string
  status?: string
  contactAttempts: number
  actionHint?: string
  nextAction?: string
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
  stage?: string
  resumeRequested?: boolean
  nextAction?: string
  actionHint?: string
}

export interface WorkflowPayload {
  generatedAt: string
  folderRoot?: string
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
  }
}

export const STAGE_ORDER: PipelineStageId[] = [
  'clarify',
  'send_resume',
  'wait_schedule',
  'decide_apply',
]
