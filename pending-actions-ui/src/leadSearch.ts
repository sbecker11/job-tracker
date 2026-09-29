/** Shared lead search — one box above the tabs, always searches every tab. */

export type SearchableLead = {
  company?: string
  title?: string
  recruiterName?: string
  recruiterEmail?: string
  recruiterPhone?: string
  duplicateOfCompany?: string
  duplicateOfTitle?: string
}

export function normalizeQuery(query: string): string {
  return query.trim().toLowerCase()
}

export function matchesLeadQuery(lead: SearchableLead, q: string): boolean {
  if (!q) return true
  return (
    (lead.company || '').toLowerCase().includes(q) ||
    (lead.title || '').toLowerCase().includes(q) ||
    (lead.recruiterName || '').toLowerCase().includes(q) ||
    (lead.recruiterEmail || '').toLowerCase().includes(q) ||
    (lead.recruiterPhone || '').toLowerCase().includes(q) ||
    (lead.duplicateOfCompany || '').toLowerCase().includes(q) ||
    (lead.duplicateOfTitle || '').toLowerCase().includes(q)
  )
}
