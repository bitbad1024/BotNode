/**
 * 工作流设置弹窗：编辑器的一套皮（头部工具栏 + 分段正文 + 底部动作），尺寸只有它一小块。
 *
 * 现在只有一项设置 —— **实例策略**（单实例 / 多实例），管的是定时触发那一拍：上一次还没跑完、
 * 到点又到点时，是跳过本次还是开新实例叠着跑（后端登记 cron 时交给调度器）。
 *
 * 加新设置：往 body 里再放一个 `<section className={styles.section}>`（一个设置一块），
 * 请求体放在 `WorkflowSettings` 里加字段即可 —— 渲染、保存、提示这些结构都不用动。
 */
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { setWorkflowSettings, type WorkflowData } from './workflowApi'
import { ApiRequestError } from '../../lib/http'
import { useToast } from '../../common/Toast'
import { IconClose, IconSettings } from '../../common/icons'
import styles from './WorkflowSettingsDialog.module.css'

interface Props {
  /** 要设置的那条工作流（列表里那一行，取当前值做初值） */
  workflow: WorkflowData
  onClose: () => void
  /** 保存成功回调（父组件就地替换列表里那一行，不整页刷新） */
  onSaved: (updated: WorkflowData) => void
}

export default function WorkflowSettingsDialog({ workflow, onClose, onSaved }: Props) {
  const { pushToast } = useToast()
  const [multiInstance, setMultiInstance] = useState(workflow.multi_instance)
  const [saving, setSaving] = useState(false)

  // Esc 关闭（保存中不关，免得到一半被关掉）
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape' && !saving) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [saving, onClose])

  const dirty = multiInstance !== workflow.multi_instance

  async function save() {
    setSaving(true)
    try {
      const { data } = await setWorkflowSettings(workflow.id, { multi_instance: multiInstance })
      pushToast('success', `设置已保存：${data.multi_instance ? '多实例' : '单实例'}`)
      onSaved(data)
      onClose()
    } catch (err) {
      pushToast('error', err instanceof ApiRequestError ? err.message : '保存设置失败')
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
        aria-label="工作流设置"
        onClick={(event) => event.stopPropagation()}
      >
        <div className={styles.head}>
          <h3 className={styles.title}>
            <IconSettings size={16} />
            工作流设置
          </h3>
          <button className="icon-btn" onClick={onClose} disabled={saving} title="关闭">
            <IconClose size={17} />
          </button>
        </div>

        <div className={styles.body}>
          <p className={styles.subject}>{workflow.name}</p>

          <section className={styles.section}>
            <div className={styles.sectionHead}>
              <h4 className={styles.sectionTitle}>实例策略</h4>
              <span className={styles.sectionNote}>只管定时触发</span>
            </div>
            <p className={styles.sectionDesc}>到点时上一次还没跑完，这一次怎么办。</p>
            <div className={styles.choices}>
              <button
                type="button"
                className={`${styles.choice} ${multiInstance ? '' : styles.choiceOn}`}
                aria-pressed={!multiInstance}
                onClick={() => setMultiInstance(false)}
              >
                <span className={styles.choiceTitle}>
                  <span className={styles.radio} />
                  单实例
                </span>
                <span className={styles.choiceDesc}>上一次还没跑完就跳过本次（缺省）</span>
              </button>
              <button
                type="button"
                className={`${styles.choice} ${multiInstance ? styles.choiceOn : ''}`}
                aria-pressed={multiInstance}
                onClick={() => setMultiInstance(true)}
              >
                <span className={styles.choiceTitle}>
                  <span className={styles.radio} />
                  多实例
                </span>
                <span className={styles.choiceDesc}>到点就开新实例，允许叠着跑</span>
              </button>
            </div>
          </section>
        </div>

        <div className={styles.foot}>
          <span className={styles.hint}>
            {dirty
              ? workflow.enabled
                ? '保存后即时生效（这条正在跑）'
                : '保存后，拨运行开关时生效'
              : '改哪一项就存哪一项'}
          </span>
          <div className={styles.footActions}>
            <button className="btn" onClick={onClose} disabled={saving}>
              取消
            </button>
            <button
              className="btn btn-primary"
              onClick={() => void save()}
              disabled={saving || !dirty}
            >
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
