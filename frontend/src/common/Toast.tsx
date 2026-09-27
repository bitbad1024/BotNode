/**
 * 极简全局轻提示（Context）：任何组件可 pushToast，由 ToastViewport 渲染。
 * 不使用系统 alert。
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { IconCheck, IconAlert, IconInfo } from './icons'

export type ToastType = 'success' | 'error' | 'info'

interface ToastItem {
  id: number
  type: ToastType
  message: string
  /** 自动消失时长（毫秒）；0 = 常驻，需手动关闭 */
  duration: number
  /** 正在退场：播放退场动画，动画结束才从列表移除 */
  leaving: boolean
}

/** 同屏最多保留的条数，超出丢最旧的——连续操作不至于堆成一长条。 */
const MAX_TOASTS = 4
/** 退场动画时长（毫秒），需与 components.css 的 .toast-leaving 对齐。 */
const LEAVE_MS = 200

interface ToastContextValue {
  toasts: ToastItem[]
  pushToast: (type: ToastType, message: string, duration?: number) => void
  dismissToast: (id: number) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

let sequence = 0

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  // 已安排移除的 id：退场动画一到点就从列表摘掉（幂等，只排一次）
  const leavingRef = useRef<Set<number>>(new Set())

  // 移除一律先标 leaving 播退场动画，动画结束后再真正移除
  const dismissToast = useCallback((id: number) => {
    setToasts((prev) =>
      prev.map((t) => (t.id === id ? { ...t, leaving: true } : t)),
    )
  }, [])

  const pushToast = useCallback(
    (type: ToastType, message: string, duration = 3600) => {
      const id = ++sequence
      setToasts((prev) => {
        const next = [...prev, { id, type, message, duration, leaving: false }]
        // 超出上限：把最旧的几条标记退场
        const alive = next.filter((t) => !t.leaving)
        if (alive.length <= MAX_TOASTS) return next
        const drop = new Set(
          alive.slice(0, alive.length - MAX_TOASTS).map((t) => t.id),
        )
        return next.map((t) => (drop.has(t.id) ? { ...t, leaving: true } : t))
      })
    },
    [],
  )

  // 退场动画一到点，从列表移除
  useEffect(() => {
    for (const t of toasts) {
      if (!t.leaving || leavingRef.current.has(t.id)) continue
      leavingRef.current.add(t.id)
      window.setTimeout(() => {
        leavingRef.current.delete(t.id)
        setToasts((prev) => prev.filter((x) => x.id !== t.id))
      }, LEAVE_MS)
    }
  }, [toasts])

  return (
    <ToastContext.Provider value={{ toasts, pushToast, dismissToast }}>
      {children}
      <ToastViewport />
    </ToastContext.Provider>
  )
}

export function useToast() {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast 必须在 ToastProvider 内使用')
  return ctx
}

const ICONS = {
  success: IconCheck,
  error: IconAlert,
  info: IconInfo,
}

function ToastViewport() {
  const { toasts, dismissToast } = useToast()

  return (
    <div className="toast-layer" aria-live="polite">
      {toasts.map((t) => (
        <ToastItemView key={t.id} item={t} onDismiss={dismissToast} />
      ))}
    </div>
  )
}

/**
 * 单条提示：自己管自动消失计时——悬停暂停（与进度条动画同步），到点走退场动画。
 */
function ToastItemView({
  item,
  onDismiss,
}: {
  item: ToastItem
  onDismiss: (id: number) => void
}) {
  const { id, type, message, duration, leaving } = item
  const Glyph = ICONS[type]
  // 剩余时长 / 本轮起点，用于「悬停暂停 -> 移开续走」
  const remainRef = useRef(duration)
  const startRef = useRef(0)
  const timerRef = useRef<number | undefined>(undefined)

  const clearTimer = useCallback(() => {
    if (timerRef.current !== undefined) {
      window.clearTimeout(timerRef.current)
      timerRef.current = undefined
    }
  }, [])

  const resume = useCallback(() => {
    if (duration <= 0) return
    // 悬停恰好卡在「计时器已到期、回调未执行」的瞬间：剩余已耗尽，
    // 不能再绕过去排队（否则这条 toast 会永久驻留），直接退场。
    if (remainRef.current <= 0) {
      onDismiss(id)
      return
    }
    startRef.current = Date.now()
    timerRef.current = window.setTimeout(() => onDismiss(id), remainRef.current)
  }, [duration, id, onDismiss])

  const pause = useCallback(() => {
    if (timerRef.current === undefined) return
    clearTimer()
    remainRef.current = Math.max(
      0,
      remainRef.current - (Date.now() - startRef.current),
    )
  }, [clearTimer])

  useEffect(() => {
    resume()
    return clearTimer
  }, [resume, clearTimer])

  return (
    <div
      className={`toast toast-${type} ${leaving ? 'toast-leaving' : ''}`}
      onMouseEnter={pause}
      onMouseLeave={resume}
    >
      <span className="toast-icon">
        <Glyph size={17} />
      </span>
      <span className="toast-msg">{message}</span>
      <button
        className="toast-close"
        aria-label="关闭"
        onClick={() => onDismiss(id)}
      >
        ×
      </button>
      {duration > 0 && (
        <span
          className="toast-progress"
          style={{ animationDuration: `${duration}ms` }}
        />
      )}
    </div>
  )
}
