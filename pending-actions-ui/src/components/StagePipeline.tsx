import type { PipelineStage, PipelineStageId } from '../types'

interface Props {
  stages: PipelineStage[]
  active: PipelineStageId
  onSelect: (id: PipelineStageId) => void
}

export function StagePipeline({ stages, active, onSelect }: Props) {
  return (
    <nav className="pipeline" aria-label="Lead workflow stages">
      {stages.map((stage, i) => {
        const isActive = stage.id === active
        return (
          <div key={stage.id} className="pipeline-step-wrap">
            {i > 0 && <div className="pipeline-arrow" aria-hidden="true" />}
            <button
              type="button"
              className={`pipeline-step${isActive ? ' active' : ''}${stage.count ? '' : ' empty'}`}
              onClick={() => onSelect(stage.id as PipelineStageId)}
            >
              <span className="pipeline-label">{stage.label}</span>
              <span className="pipeline-count">{stage.count}</span>
              <span className="pipeline-action">{stage.action}</span>
            </button>
          </div>
        )
      })}
    </nav>
  )
}
