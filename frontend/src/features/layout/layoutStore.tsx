/**
 * 布局状态：
 * - collapsed：桌面端侧边栏折叠（完整 ↔ 仅图标），持久化到 localStorage；
 * - mobileOpen：小屏端抽屉开关（不持久化，默认关闭）。
 *
 * 两者互不干扰：桌面只看 collapsed，小屏只看 mobileOpen。
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react'

const COLLAPSE_KEY = 'nacho.sidebar.collapsed'
/** 小屏断点（与 CSS 媒体查询保持一致，px）：<768 为手机，走抽屉；≥768 为桌面式侧栏。 */
export const MOBILE_BREAKPOINT = 768

interface LayoutContextValue {
  collapsed: boolean
  toggleCollapsed: () => void
  mobileOpen: boolean
  openMobile: () => void
  closeMobile: () => void
  toggleMobile: () => void
}

const LayoutContext = createContext<LayoutContextValue | null>(null)

function getInitialCollapsed(): boolean {
  return localStorage.getItem(COLLAPSE_KEY) === '1'
}

export function LayoutProvider({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState<boolean>(getInitialCollapsed)
  const [mobileOpen, setMobileOpen] = useState(false)

  // 折叠偏好持久化
  useEffect(() => {
    localStorage.setItem(COLLAPSE_KEY, collapsed ? '1' : '0')
  }, [collapsed])

  // 切回大屏时自动收起移动抽屉
  useEffect(() => {
    const mq = window.matchMedia(`(min-width: ${MOBILE_BREAKPOINT}px)`)
    const onChange = (e: MediaQueryListEvent) => {
      if (e.matches) setMobileOpen(false)
    }
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  const toggleCollapsed = useCallback(() => setCollapsed((v) => !v), [])
  const openMobile = useCallback(() => setMobileOpen(true), [])
  const closeMobile = useCallback(() => setMobileOpen(false), [])
  const toggleMobile = useCallback(() => setMobileOpen((v) => !v), [])

  return (
    <LayoutContext.Provider
      value={{
        collapsed,
        toggleCollapsed,
        mobileOpen,
        openMobile,
        closeMobile,
        toggleMobile,
      }}
    >
      {children}
    </LayoutContext.Provider>
  )
}

export function useLayout() {
  const ctx = useContext(LayoutContext)
  if (!ctx) throw new Error('useLayout 必须在 LayoutProvider 内使用')
  return ctx
}
