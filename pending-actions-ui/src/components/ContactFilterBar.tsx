import { LEAD_STATUS_OPTIONS } from './ManageLeadStatus'
import type { ContactFilter, LeadSort, StatusFilter } from '../priorityQueue'

interface StageOption {
  id: ContactFilter
  label: string
  count: number
  hint: string
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
  return (
    <div className="list-filters">
      <label className="status-filter">
        <span>Status</span>
        <select
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
      <nav className="filter-bar" aria-label="Filter by task">
        {stageOptions.map((opt) => {
          const isActive = opt.id === activeStage
          return (
            <button
              key={opt.id}
              type="button"
              className={`filter-chip${isActive ? ' active' : ''}${opt.count ? '' : ' empty'}`}
              onClick={() => onSelectStage(opt.id)}
              title={opt.hint}
            >
              <span className="filter-label">{opt.label}</span>
              <span className="filter-count">{opt.count}</span>
            </button>
          )
        })}
      </nav>
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
