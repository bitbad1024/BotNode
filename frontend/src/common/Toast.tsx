/**
 * 极简全局轻提示（Context）：任何组件可 pushToast，由 ToastViewport 渲染。
 * 不使用系统 alert。
 */
import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from 'react'
import { IconCheck, IconAlert, IconInfo } from './icons'

export type ToastType = 'success' | 'error' | 'info'

interface ToastItem {
  id: number
  type: ToastType
  message: string
}

interface ToastContextValue {
  toasts: ToastItem[]
  pushToast: (type: ToastType, message: string, duration?: number) => void
  dismissToast: (id: number) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

let sequence = 0

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([])

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const pushToast = useCallback(
    (type: ToastType, message: string, duration = 3600) => {
      const id = ++sequence
      setToasts((prev) => [...prev, { id, type, message }])
      window.setTimeout(() => dismissToast(id), duration)
    },
    [dismissToast],
  )

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
      {toasts.map((t) => {
        const Glyph = ICONS[t.type]
        return (
          <div key={t.id} className={`toast toast-${t.type}`}>
            <span className="toast-icon">
              <Glyph size={17} />
            </span>
            <span className="toast-msg">{t.message}</span>
            <button
              className="toast-close"
              aria-label="关闭"
              onClick={() => dismissToast(t.id)}
            >
              ×
            </button>
          </div>
        )
      })}
    </div>
  )
}
