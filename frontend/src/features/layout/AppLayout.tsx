/**
 * 后台主框架：左侧栏 + 顶栏 + 主内容。
 * 桌面：侧栏为文档流内的固定宽度（可折叠成图标条）；
 * 小屏：侧栏变抽屉（fixed + 遮罩），由顶栏汉堡开关。
 */
import { Outlet } from 'react-router-dom'
import { LayoutProvider, useLayout } from './layoutStore'
import Sidebar from './Sidebar'
import Topbar from './Topbar'
import styles from './AppLayout.module.css'

function AppLayoutInner() {
  const { collapsed, mobileOpen, closeMobile } = useLayout()

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
      <AppLayoutInner />
    </LayoutProvider>
  )
}
