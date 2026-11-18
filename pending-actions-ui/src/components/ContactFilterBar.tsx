import { useEffect, useRef } from 'react'
import { LEAD_STATUS_OPTIONS } from './ManageLeadStatus'
import type { ContactFilter, LeadSort, StatusFilter } from '../priorityQueue'

/** Chrome restores the last <select> choice after a reload, after React has
 * already set the default. Write the React value back once that restore runs. */
function useHeldSelect<T extends string>(value: T) {
  const ref = useRef<HTMLSelectElement>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const apply = () => {
      if (el.value !== value) el.value = value
    }
    apply()
    const timer = window.setTimeout(apply, 0)
    window.addEventListener('pageshow', apply)
    return () => {
      window.clearTimeout(timer)
      window.removeEventListener('pageshow', apply)
    }
  }, [value])
  return ref
}

interface StageOption {
  id: ContactFilter
  label: string
  count: number
}

interface StatusOption {
  value: StatusFilter
  label: string
  count: number
}

export const LEAD_SORT_OPTIONS: { value: LeadSort; label: string }[] = [
  { value: 'priority', label: 'Task priority' },
  { value: 'age_desc', label: 'Age decreasing (oldest first)' },
  { value: 'age_asc', label: 'Age increasing (newest first)' },
]

interface Props {
  stageOptions: StageOption[]
  activeStage: ContactFilter
  onSelectStage: (id: ContactFilter) => void
  statusOptions: StatusOption[]
  activeStatus: StatusFilter
  onSelectStatus: (id: StatusFilter) => void
  activeSort: LeadSort
  onSelectSort: (id: LeadSort) => void
}

export function ContactFilterBar({
  stageOptions,
  activeStage,
  onSelectStage,
  statusOptions,
  activeStatus,
  onSelectStatus,
  activeSort,
  onSelectSort,
}: Props) {
  const stageRef = useHeldSelect(activeStage)
  const statusRef = useHeldSelect(activeStatus)
  const sortRef = useHeldSelect(activeSort)
  return (
    <div className="list-filters">
      <label className="status-filter">
        <span>Section</span>
        <select
          ref={stageRef}
          autoComplete="off"
          value={activeStage}
          onChange={(e) => onSelectStage(e.target.value as ContactFilter)}
          aria-label="Filter leads by section"
        >
          {stageOptions.map((opt) => (
            <option key={opt.id} value={opt.id}>
              {opt.label} ({opt.count})
            </option>
          ))}
        </select>
      </label>
      <label className="status-filter">
        <span>Status</span>
        <select
          ref={statusRef}
          autoComplete="off"
          value={activeStatus}
          onChange={(e) => onSelectStatus(e.target.value)}
          aria-label="Filter leads by status"
        >
          {statusOptions.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label} ({opt.count})
            </option>
          ))}
        </select>
      </label>
      <label className="status-filter">
        <span>Sort</span>
        <select
          ref={sortRef}
          autoComplete="off"
          value={activeSort}
          onChange={(e) => onSelectSort(e.target.value as LeadSort)}
          aria-label="Sort leads"
        >
          {LEAD_SORT_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </label>
    </div>
  )
}

export function statusFilterOptions(
  counts: Record<string, number>,
  activeCount: number,
  anyCount: number,
): StatusOption[] {
  const known = new Set(LEAD_STATUS_OPTIONS.map((o) => o.value))
  const extras = Object.keys(counts)
    .filter((value) => !known.has(value) && value)
    .sort()
  return [
    { value: 'active', label: 'Active work', count: activeCount },
    { value: 'any', label: 'Any status', count: anyCount },
    ...LEAD_STATUS_OPTIONS.filter((o) => counts[o.value]).map((o) => ({
      value: o.value,
      label: o.label,
      count: counts[o.value],
    })),
    ...extras.map((value) => ({
      value,
      label: value,
      count: counts[value],
    })),
  ]
}
