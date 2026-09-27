/**
 * 新建工作流弹窗：只问一个名字（同账号下唯一），建好交给列表自己刷新。
 *
 * 列表页把「新建」收进了列表卡片右上角（不再单独占一块面板），弹窗因此只干一件事：
 * 要个名字 -> POST /api/workflows -> 成功就把新名字递回去。
 *
 * 交互与设置弹窗同一套：Esc 关闭（提交中不关）、点遮罩关闭、回车即提交。
 */
import { useEffect, useState, type FormEvent } from 'react'
import { createPortal } from 'react-dom'
import { createWorkflow } from './workflowApi'
import { ApiRequestError } from '../../lib/http'
import { useToast } from '../../common/Toast'
import { IconClose, IconPlus } from '../../common/icons'
import styles from './WorkflowCreateDialog.module.css'

interface Props {
  onClose: () => void
  /** 创建成功：把新工作流的名字递给列表（由它决定整页重拉还是就地插入） */
  onCreated: (name: string) => void
}

export default function WorkflowCreateDialog({ onClose, onCreated }: Props) {
  const { pushToast } = useToast()
  const [name, setName] = useState('')
  const [saving, setSaving] = useState(false)

  // Esc 关闭（提交中不关，免得建到一半被关掉）
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape' && !saving) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [saving, onClose])

  async function submit(event: FormEvent) {
    event.preventDefault()
    const value = name.trim()
    if (!value || saving) return
    setSaving(true)
    try {
      const { data } = await createWorkflow(value)
      pushToast('success', `工作流「${data.name}」已创建`)
      onCreated(data.name)
      onClose()
    } catch (err) {
      pushToast('error', err instanceof ApiRequestError ? err.message : '创建失败')
    } finally {
      setSaving(false)
    }
  }

  return createPortal(
    <div className={styles.overlay} onClick={saving ? undefined : onClose}>
      <div
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-label="新建工作流"
        onClick={(event) => event.stopPropagation()}
      >
        <div className={styles.head}>
          <h3 className={styles.title}>
            <IconPlus size={16} />
            新建工作流
          </h3>
          <button className="icon-btn" onClick={onClose} disabled={saving} title="关闭">
            <IconClose size={17} />
          </button>
        </div>

        <form onSubmit={submit}>
          <div className={styles.body}>
            <label className={styles.field}>
              <span className={styles.label}>名称</span>
              <input
                autoFocus
                className={styles.input}
                value={name}
                maxLength={128}
                placeholder="例如：每日巡检播报"
                disabled={saving}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            <p className={styles.hint}>同账号下名称不能重复；建好后在列表里点「编辑」进画布编排。</p>
          </div>

          <div className={styles.foot}>
            <div className={styles.footActions}>
              <button className="btn" type="button" onClick={onClose} disabled={saving}>
                取消
              </button>
              <button
                className="btn btn-primary"
                type="submit"
                disabled={saving || !name.trim()}
              >
                {saving ? '创建中…' : '创建'}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>,
    document.body,
  )
}
