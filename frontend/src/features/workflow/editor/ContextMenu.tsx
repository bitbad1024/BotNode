/**
 * 节点右键菜单：视口坐标定位（``position: fixed``），点别处 / Esc 关闭（关闭逻辑在父组件）。
 *
 * 菜单内容只有一项 —— 删除（可能是一组）；以后加项就往里放按钮。
 */
import { IconTrash } from '../../../common/icons'
import styles from '../WorkflowEditor.module.css'

export interface ContextMenuProps {
  x: number
  y: number
  ids: string[]
  menuRef: React.RefObject<HTMLDivElement>
  onDelete: (ids: string[]) => void
}

export function ContextMenu({ x, y, ids, menuRef, onDelete }: ContextMenuProps) {
  return (
    <div
      ref={menuRef}
      className={styles.ctxMenu}
      style={{
        left: Math.max(8, Math.min(x, window.innerWidth - 200)),
        top: Math.max(8, Math.min(y, window.innerHeight - 52)),
      }}
      onContextMenu={(e) => e.preventDefault()}
    >
      <button
        className={`${styles.ctxMenuItem} ${styles.ctxMenuItemDanger}`}
        onClick={() => onDelete(ids)}
      >
        <IconTrash size={14} />
        {ids.length > 1 ? `删除选中的 ${ids.length} 个节点` : '删除节点'}
      </button>
    </div>
  )
}
