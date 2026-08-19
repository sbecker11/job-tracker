import type { ContactFilter } from '../priorityQueue'
import type { PipelineStageId } from '../types'

interface FilterOption {
  id: ContactFilter
  label: string
  count: number
  hint: string
}

interface Props {
  options: FilterOption[]
  active: ContactFilter
  onSelect: (id: ContactFilter) => void
}

export function ContactFilterBar({ options, active, onSelect }: Props) {
  return (
    <nav className="filter-bar" aria-label="Filter contact priority">
      {options.map((opt) => {
        const isActive = opt.id === active
        return (
          <button
            key={opt.id}
            type="button"
            className={`filter-chip${isActive ? ' active' : ''}${opt.count ? '' : ' empty'}`}
            onClick={() => onSelect(opt.id)}
            title={opt.hint}
          >
            <span className="filter-label">{opt.label}</span>
            <span className="filter-count">{opt.count}</span>
          </button>
        )
      })}
    </nav>
  )
}

export type { PipelineStageId }
