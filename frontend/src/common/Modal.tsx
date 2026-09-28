/**
 * 通用弹窗：内容型 / 表单型对话框（没有 ConfirmDialog 那种确认按钮语义）。
 *
 * 实现要点与 ConfirmDialog 一致（挂 body、层级 900、Esc / 遮罩可关），
 * 区别是底部操作区（footer）由调用方自由拼装，适合签发表单、详情查看这类弹窗。
 *
 * 用法::
 *
 *     const [open, setOpen] = useState(false)
 *     ...
 *     {open && (
 *       <Modal title="添加机器人" onClose={() => setOpen(false)}>
 *         <form>…</form>
 *         <footer>…</footer>
 *       </Modal>
 *     )}
 */
import { useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { IconClose } from './icons'
import styles from './Modal.module.css'

export interface ModalProps {
  title: ReactNode
  /** 正文（表单 / 详情内容） */
  children: ReactNode
  /** 底部操作区（可选） */
  footer?: ReactNode
  /** 请求进行中：按钮禁用、遮罩不点关、Esc 不关 */
  busy?: boolean
  onClose: () => void
}

export function Modal({ title, children, footer, busy = false, onClose }: ModalProps) {
  // Esc 关闭（处理中不关）
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape' && !busy) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [busy, onClose])

  return createPortal(
    <div className={styles.overlay} onClick={busy ? undefined : onClose}>
      <div
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <div className={styles.head}>
          <h3 className={styles.title}>{title}</h3>
          <button
            type="button"
            className="icon-btn"
            onClick={onClose}
            disabled={busy}
            aria-label="关闭"
          >
            <IconClose size={17} />
          </button>
        </div>
        <div className={styles.body}>{children}</div>
        {footer && <div className={styles.foot}>{footer}</div>}
      </div>
    </div>,
    document.body,
  )
}
