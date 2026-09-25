/**
 * 头像图片：从全局 authStore 读 blob URL（由 AppLayout 拉好）。
 *
 * 不再自己发请求 —— 头像数据是全局状态，所有组件共享同一份。
 */
import { useAuth } from './authStore'
import styles from './AvatarImage.module.css'

interface AvatarImageProps {
  /** 没头像时显示的首字母 */
  initial: string
  /** 尺寸：sm(30px) / md(40px) / lg(52px) / xl(80px) */
  size?: 'sm' | 'md' | 'lg' | 'xl'
  /** 额外的 className */
  className?: string
}

const SIZE_CLASS: Record<string, string> = {
  sm: styles.sm,
  md: styles.md,
  lg: styles.lg,
  xl: styles.xl,
}

export default function AvatarImage({ initial, size = 'md', className = '' }: AvatarImageProps) {
  const { state } = useAuth()
  const blobUrl = state.avatarBlobUrl
  const sizeClass = SIZE_CLASS[size] ?? SIZE_CLASS.md

  if (blobUrl) {
    return (
      <img
        className={`${styles.img} ${sizeClass} ${className}`}
        src={blobUrl}
        alt="头像"
      />
    )
  }

  return (
    <span className={`${styles.placeholder} ${sizeClass} ${className}`}>
      {initial}
    </span>
  )
}
