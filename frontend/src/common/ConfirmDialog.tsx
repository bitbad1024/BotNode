/**
 * 通用确认弹窗：破坏性 / 不可逆的操作统一走它（吊销令牌、下线设备、删除工作流、删头像…）。
 *
 * 为什么**必须**这样写：
 *
 * * **挂到 body**：页面根元素带 ``.rise`` 入场动画（``transform``），``position: fixed``
 *   会以它为包含块 —— 遮罩只盖住内容那一列，侧边栏与顶栏露在外面。送到 body 才真的盖满视口；
 * * **层级 900**：侧边栏 100、顶栏抽屉 101，写低了会被它们压在下面，看着也像没盖住；
 * * **焦点默认在「取消」**：危险动作不该被一次回车误触；Esc 也能关（提交中不关）。
 *
 * 用法（父组件拿一份「待确认」状态，条件渲染即可）::
 *
 *     const [pending, setPending] = useState<Target | null>(null)
 *     ...
 *     {pending && (
 *       <ConfirmDialog
 *         title="吊销这个令牌？"
 *         body={<>吊销后用它连着的客户端会立刻断开，且不可恢复。</>}
 *         confirmText="吊销"
 *         busy={busy}
 *         onCancel={() => setPending(null)}
 *         onConfirm={() => void revoke(pending)}
 *       />
 *     )}
 */
import { useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { IconClose } from './icons'
import styles from './ConfirmDialog.module.css'

export interface ConfirmDialogProps {
  title: string
  /** 正文：要说清后果（不可逆？影响谁？） */
  body: ReactNode
  /** 确认按钮文案（写动作本身，别写「确定」） */
  confirmText: string
  cancelText?: string
  /** 危险动作（红按钮）；普通确认传 false（主色按钮） */
  danger?: boolean
  /** 请求进行中：按钮禁用、遮罩不点关、Esc 不关 */
  busy?: boolean
  onCancel: () => void
  onConfirm: () => void
}

export function ConfirmDialog({
  title,
  body,
  confirmText,
  cancelText = '取消',
  danger = true,
  busy = false,
  onCancel,
  onConfirm,
}: ConfirmDialogProps) {
  // Esc 关闭（处理中不关，免得吊销到一半被关掉）
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape' && !busy) onCancel()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [busy, onCancel])

  return createPortal(
    <div className={styles.overlay} onClick={busy ? undefined : onCancel}>
      <div
        className={styles.dialog}
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className={styles.head}>
          <h3 className={styles.title}>{title}</h3>
          <button
            type="button"
            className="icon-btn"
            onClick={onCancel}
            disabled={busy}
            aria-label="关闭"
          >
            <IconClose size={17} />
          </button>
        </div>
        <div className={styles.body}>{body}</div>
        <div className={styles.foot}>
          {/* 危险动作：焦点默认落在「取消」，回车 / 空格不会误触 */}
          <button type="button" className="btn" onClick={onCancel} disabled={busy} autoFocus>
            {cancelText}
          </button>
          <button
            type="button"
            className={`btn ${danger ? styles.dangerBtn : styles.primaryBtn}`}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? (
              <span className={styles.busyInner}>
                <span className="spinner" />
                处理中…
              </span>
            ) : (
              confirmText
            )}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
