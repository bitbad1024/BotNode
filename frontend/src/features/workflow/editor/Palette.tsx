/**
 * 节点面板（悬浮在画布左侧）：点一下直接添加，按住拖进画布则在松手处落子。
 *
 * 面板项与底部端口类型图例**全部来自后端目录**（见 :func:`catalog.installCatalog`）——
 * 顺序、中文名、语义分类、端口配色都在里面，前端只补节点颜色并按分类分组。
 */
import {
  CATEGORY_LABELS,
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
          <div className={styles.paletteGroupTitle}>{CATEGORY_LABELS[category] ?? category}</div>
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
                {def.label}
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
