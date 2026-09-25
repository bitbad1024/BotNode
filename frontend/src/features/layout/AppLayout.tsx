/**
 * 后台主框架：左侧栏 + 顶栏 + 主内容。
 * 桌面：侧栏为文档流内的固定宽度（可折叠成图标条）；
 * 小屏：侧栏变抽屉（fixed + 遮罩），由顶栏汉堡开关。
 */
import { useEffect } from 'react'
import { Outlet } from 'react-router-dom'
import { LayoutProvider, useLayout } from './layoutStore'
import { DebugProvider } from '../onebot/debugStore'
import { useAuth } from '../auth/authStore'
import { fetchProfile } from '../auth/profileApi'
import { http } from '../../lib/http'
import Sidebar from './Sidebar'
import Topbar from './Topbar'
import styles from './AppLayout.module.css'

function AppLayoutInner() {
  const { collapsed, mobileOpen, closeMobile } = useLayout()
  const { state, dispatch } = useAuth()

  // 登录接口返回的 user 不含头像信息，进后台后拉一次 /profile 补齐
  useEffect(() => {
    if (!state.token) return
    let cancelled = false
    fetchProfile()
      .then(({ data }) => {
        if (cancelled) return
        dispatch({ type: 'SET_USER', user: data })
        // 拉完资料再拉头像 blob，写入全局 store（所有组件共享）
        if (data.has_avatar && data.avatar_url) {
          http
            .getBlob(data.avatar_url)
            .then((blob) => {
              if (!cancelled) {
                dispatch({ type: 'SET_AVATAR_BLOB', url: URL.createObjectURL(blob) })
              }
            })
            .catch(() => { /* 头像拉不到就用首字母 */ })
        } else {
          dispatch({ type: 'SET_AVATAR_BLOB', url: undefined })
        }
      })
      .catch(() => {
        /* 拉不到就保留现有资料（网络抖动时不打扰用户） */
      })
    return () => {
      cancelled = true
    }
    // 只在进后台时拉一次；后续改动（改昵称/头像）由操作方自己 SET_USER
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.token])

  return (
    <div className={`${styles.layout} ${collapsed ? styles.collapsed : ''}`}>
      <Sidebar />
      {mobileOpen && (
        <div
          className={styles.overlay}
          onClick={closeMobile}
          aria-hidden="true"
        />
      )}
      <div className={styles.main}>
        <Topbar />
        <main className={styles.content}>
          <Outlet />
        </main>
      </div>
    </div>
  )
}

export default function AppLayout() {
  return (
    <LayoutProvider>
      {/* WS 调试会话挂在主框架上：切页面不卸载，连接与记录都保留 */}
      <DebugProvider>
        <AppLayoutInner />
      </DebugProvider>
    </LayoutProvider>
  )
}
