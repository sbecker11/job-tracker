import { createContext } from 'react'
import type { DeclineReason } from '../types'

/** The payload's `declineReasons` (job_tracker.pipeline.decline), shared by the
 * Decline form and Manage Lead's "Rejected" reason picker. "Other" is last. */
export const DeclineReasonsContext = createContext<DeclineReason[]>([])
