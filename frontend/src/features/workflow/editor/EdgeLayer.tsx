/**
 * 连线层：一张铺在画布上的 SVG，按 ``nodeById`` + ``posOf`` 画所有边。
 *
 * 坐标从**传入的节点表与坐标来源**算，所以同一份绘制逻辑也服务粘贴虚影 —— 那时传的是
 * 剪贴板里的节点与偏移后的坐标。
 *
 * 每条边中点有个「删除」小圆点（悬停画布才显出来，见 CSS）；``onDelete`` 不传就不画它
 * （虚影层没有删除这一说）。
 */
import {
  edgeColor,
  edgeCoords,
  edgeCurve,
  type Point,
  type WorkflowEdge,
  type WorkflowNode,
} from './catalog'
import styles from '../WorkflowEditor.module.css'

export interface EdgeLayerProps {
  edges: WorkflowEdge[]
  nodeById: Map<string, WorkflowNode>
  posOf: (id: string) => Point | undefined
  /** 点中点的删除圆点（真实连线才有） */
  onDelete?: (edge: WorkflowEdge) => void
  /** 半透明（粘贴虚影用） */
  faint?: boolean
  /** 正在拉的临时连线：起点 -> 鼠标（虚线） */
  pending?: { path: string; color: string } | null
}

export function EdgeLayer({
  edges,
  nodeById,
  posOf,
  onDelete,
  faint = false,
  pending = null,
}: EdgeLayerProps) {
  return (
    <svg className={styles.edges} style={{ pointerEvents: faint ? 'none' : undefined }}>
      {edges.map((edge, i) => {
        const c = edgeCoords(edge, nodeById, posOf)
        if (!c) return null
        return (
          <g key={i}>
            <path
              d={edgeCurve(c.x1, c.y1, c.x2, c.y2)}
              fill="none"
              stroke={edgeColor(edge, nodeById)}
              strokeWidth="2"
              strokeLinecap="round"
              strokeOpacity={faint ? '0.6' : undefined}
            />
            {onDelete && (
              <circle
                cx={(c.x1 + c.x2) / 2}
                cy={(c.y1 + c.y2) / 2}
                r="7"
                fill="var(--surface)"
                stroke="var(--danger)"
                strokeWidth="1.5"
                className={styles.edgeDelete}
                onClick={() => onDelete(edge)}
              />
            )}
          </g>
        )
      })}
      {pending && (
        <path
          d={pending.path}
          fill="none"
          stroke={pending.color}
          strokeWidth="2"
          strokeDasharray="6 5"
          strokeLinecap="round"
        />
      )}
    </svg>
  )
}
