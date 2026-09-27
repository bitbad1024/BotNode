/**
 * 编辑器顶部工具栏：返回 / 暂存 / 校验 / 保存版本 / 发布 / 运行开关 / 面板开关 / 缩放。
 *
 * 全是按钮 + 一份工作流定义，没有自己的状态（缩放也由父组件的 ``useCanvasView`` 持有）。
 */
import { IconArrowLeft, IconCheck, IconClose, IconSave } from '../../../common/icons'
import type { WorkflowData } from '../workflowApi'
import styles from '../WorkflowEditor.module.css'

export interface ToolbarProps {
  definition: WorkflowData | null
  drafting: boolean
  validating: boolean
  saving: boolean
  switching: boolean
  /** 当前缩放百分比（按钮上显示，点击回 100%） */
  zoomPercent: number
  showPalette: boolean
  showInspector: boolean
  onClose: () => void
  onDraft: () => void
  onValidate: () => void
  onSave: () => void
  onPublish: () => void
  onToggleEnabled: () => void
  onTogglePalette: () => void
  onToggleInspector: () => void
  onResetView: () => void
}

export function Toolbar({
  definition,
  drafting,
  validating,
  saving,
  switching,
  zoomPercent,
  showPalette,
  showInspector,
  onClose,
  onDraft,
  onValidate,
  onSave,
  onPublish,
  onToggleEnabled,
  onTogglePalette,
  onToggleInspector,
  onResetView,
}: ToolbarProps) {
  return (
    <header className={styles.toolbar}>
      <button className="btn" onClick={onClose}>
        <IconArrowLeft size={15} />
        返回
      </button>
      <div className={styles.toolbarTitle}>
        工作流编辑器
        {definition?.current_ref === 'draft' ? (
          <span className={styles.verTag} title="当前查看 / 编辑的是暂存区">
            暂存区{definition.draft_updated_at ? '（已暂存）' : ''}
          </span>
        ) : (
          definition &&
          definition.current_version > 0 && (
            <span className={styles.verTag} title="当前查看的是已提交版本">
              当前 v{definition.current_version}
            </span>
          )
        )}
      </div>
      <div className={styles.toolbarActions}>
        <button className="btn" onClick={onDraft} disabled={drafting}>
          <IconSave size={14} />
          {drafting ? '暂存中…' : '暂存'}
        </button>
        <button className="btn" onClick={onValidate} disabled={validating}>
          <IconCheck size={14} />
          {validating ? '校验中…' : '校验'}
        </button>
        <button className="btn btn-primary" onClick={onSave} disabled={saving}>
          <IconSave size={14} />
          {saving ? '提交中…' : '保存版本'}
        </button>
        <button className="btn" onClick={onPublish}>
          发布
        </button>
        <button
          className={`btn ${definition?.enabled ? 'btn-primary' : ''}`}
          disabled={switching || !definition || definition.published_version === 0}
          title={
            definition?.enabled
              ? '点一下停止：不再定时触发（发布状态不变）'
              : '点一下开启：按已发布版本跑'
          }
          onClick={onToggleEnabled}
        >
          {switching ? '切换中…' : definition?.enabled ? '运行中' : '已停止'}
        </button>
        <button
          className={`btn ${showPalette ? '' : styles.toggleOff}`}
          onClick={onTogglePalette}
          title="切换节点面板"
        >
          节点
        </button>
        <button
          className={`btn ${showInspector ? '' : styles.toggleOff}`}
          onClick={onToggleInspector}
          title="切换配置面板"
        >
          配置
        </button>
        <button className="btn" onClick={onResetView} title="重置视图">
          {zoomPercent}%
        </button>
        <button className={`btn ${styles.closeBtn}`} onClick={onClose}>
          <IconClose size={16} />
        </button>
      </div>
    </header>
  )
}
