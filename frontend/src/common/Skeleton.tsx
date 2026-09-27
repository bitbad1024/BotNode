/**
 * 骨架屏占位：列表 / 卡片加载时替代 spinner，降低等待感知。
 * 纯展示组件，整体 aria-hidden，避免读屏器把占位读成内容。
 */
import type { CSSProperties } from 'react'

type SkeletonProps = {
  /** 宽度：数字按 px，字符串原样（如 '60%'） */
  width?: number | string
  /** 高度：数字按 px，默认 12 */
  height?: number | string
  /** 圆角：数字按 px，字符串原样（如 '50%'），默认继承 .skeleton 令牌 */
  radius?: number | string
  className?: string
  style?: CSSProperties
}

/** 单个骨架块，尺寸可完全自定义，用于拼装任意版式 */
export function Skeleton({
  width,
  height = 12,
  radius,
  className = '',
  style,
}: SkeletonProps) {
  return (
    <span
      className={`skeleton ${className}`.trim()}
      style={{ width, height, borderRadius: radius, ...style }}
      aria-hidden="true"
    />
  )
}

/** 列表型骨架：rows 行「头像块 + 两行文字 + 右侧操作块」，行宽带轻微错落更自然 */
export function ListSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div className="skeleton-list" aria-hidden="true">
      {Array.from({ length: rows }).map((_, i) => (
        <div className="skeleton-row" key={i}>
          <span className="skeleton skeleton-avatar" />
          <div className="skeleton-lines">
            <Skeleton width={`${38 + ((i * 9) % 22)}%`} height={13} />
            <Skeleton width={`${58 + ((i * 13) % 26)}%`} height={11} />
          </div>
          <span className="skeleton skeleton-pill" />
        </div>
      ))}
    </div>
  )
}
