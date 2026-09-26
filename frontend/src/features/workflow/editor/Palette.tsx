/**
 * 节点面板（悬浮在画布左侧）：点一下直接添加，按住拖进画布则在松手处落子。
 *
 * 面板项**全部来自后端目录**（顺序与中文名都在里面，见 :func:`catalog.installCatalog`），
 * 前端只补颜色；底部是端口类型的图例。
 */
import { PORT_COLORS, nodeDef, type NodeTypeSpec } from './catalog'
import styles from '../WorkflowEditor.module.css'

export interface PaletteProps {
  items: NodeTypeSpec[]
  /** 按下（拖进画布的起点） */
  onItemMouseDown: (event: React.MouseEvent, type: string) => void
  /** 纯点击 / 键盘触发 */
  onItemClick: (event: React.MouseEvent, type: string) => void
}

export function Palette({ items, onItemMouseDown, onItemClick }: PaletteProps) {
  return (
    <aside className={styles.palette}>
      <div className={styles.paletteTitle}>节点</div>
      {items.map((spec) => {
        const def = nodeDef(spec.type)
        return (
          <button
            key={spec.type}
            className={styles.paletteItem}
            onMouseDown={(e) => onItemMouseDown(e, spec.type)}
            onClick={(e) => onItemClick(e, spec.type)}
          >
            <span className={styles.paletteDot} style={{ background: def.color }} />
            {def.label}
          </button>
        )
      })}
      <div className={styles.legend}>
        <div className={styles.legendTitle}>端口类型</div>
        <div className={styles.legendRow}>
          <span className={styles.legendDot} style={{ background: PORT_COLORS.trigger }} />
          触发（控制流）
        </div>
        <div className={styles.legendRow}>
          <span className={styles.legendDot} style={{ background: PORT_COLORS.message }} />
          消息（数据流）
        </div>
      </div>
    </aside>
  )
}
