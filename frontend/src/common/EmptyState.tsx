/**
 * 空态占位：图标 + 标题 + 说明 + 可选操作。
 * 列表 / 表格为空时给出下一步引导，而不是一句灰字。
 */
import type { ComponentType, ReactNode } from 'react'

interface EmptyStateProps {
  /** 图标组件（来自 common/icons），按 size 渲染 */
  icon: ComponentType<{ size?: number }>
  title: string
  hint?: ReactNode
  /** 引导操作，通常是一颗按钮 */
  action?: ReactNode
}

export function EmptyState({ icon: Icon, title, hint, action }: EmptyStateProps) {
  return (
    <div className="empty-state">
      <span className="empty-state-icon">
        <Icon size={22} />
      </span>
      <span className="empty-state-title">{title}</span>
      {hint && <span className="empty-state-hint">{hint}</span>}
      {action && <div className="empty-state-action">{action}</div>}
    </div>
  )
}
