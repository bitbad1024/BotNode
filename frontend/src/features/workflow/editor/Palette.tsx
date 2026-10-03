/**
 * 节点面板（悬浮在画布左侧）：点一下直接添加，按住拖进画布则在松手处落子。
 */
import {
  categoryLabel,
  groupByCategory,
  nodeDef,
  portTypeLegend,
  type NodeTypeSpec,
} from './catalog'
import styles from '../WorkflowEditor.module.css'

export interface PaletteProps {
  items: NodeTypeSpec[]
  /** 按下（拖进画布的起点） */
  onItemMouseDown: (event: React.MouseEvent, type: string) => void
  /** 纯点击 / 键盘触发 */
  onItemClick: (event: React.MouseEvent, type: string) => void
}

export function Palette({ items, onItemMouseDown, onItemClick }: PaletteProps) {
  const groups = groupByCategory(items)
  return (
    <aside className={styles.palette}>
      <div className={styles.paletteTitle}>节点</div>
      {groups.map(([category, specs]) => (
        <div key={category} className={styles.paletteGroup}>
          <div className={styles.paletteGroupTitle}>{categoryLabel(category)}</div>
          {specs.map((spec) => {
            const def = nodeDef(spec.type)
            return (
              <button
                key={spec.type}
                className={styles.paletteItem}
                onMouseDown={(e) => onItemMouseDown(e, spec.type)}
                onClick={(e) => onItemClick(e, spec.type)}
              >
                <span className={styles.paletteDot} style={{ background: def.color }} />
                {/* 装不下时省略（见 .paletteText），悬停看完整名字 */}
                <span className={styles.paletteText} title={def.label}>
                  {def.label}
                </span>
              </button>
            )
          })}
        </div>
      ))}
      <div className={styles.legend}>
        <div className={styles.legendTitle}>端口类型</div>
        {portTypeLegend().map((portType) => (
          <div className={styles.legendRow} key={portType.type}>
            <span className={styles.legendDot} style={{ background: portType.color }} />
            {portType.label}
          </div>
        ))}
      </div>
    </aside>
  )
}
