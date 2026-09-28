/**
 * 侧边栏：桌面为可折叠导航条（完整 ↔ 仅图标），小屏为滑入抽屉。
 * 同一组件，宽度 / 显隐由 CSS 媒体查询 + 根类控制。
 */
import { NavLink } from 'react-router-dom'
import { NAV_ITEMS } from './nav'
import { useLayout } from './layoutStore'
import { IconLogo } from '../../common/icons'
import styles from './Sidebar.module.css'

export default function Sidebar() {
  const { collapsed, mobileOpen, closeMobile } = useLayout()

  return (
    <aside
      className={`${styles.sidebar} ${collapsed ? styles.isCollapsed : ''} ${
        mobileOpen ? styles.isOpen : ''
      }`}
    >
      {/* 品牌 */}
      <div className={styles.brand}>
        <span className={styles.logo} aria-hidden="true">
          <IconLogo size={17} />
        </span>
        <span className={styles.brandName}>nacho</span>
      </div>

      {/* 导航 */}
      <nav className={styles.nav}>
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon
          return (
            <NavLink
              key={item.path}
              to={item.path}
              end={item.end}
              title={item.label}
              onClick={closeMobile}
              className={({ isActive }) =>
                `${styles.navItem} ${isActive ? styles.navActive : ''}`
              }
            >
              <span className={styles.navIcon}>
                <Icon size={20} />
              </span>
              <span className={styles.navLabel}>{item.label}</span>
            </NavLink>
          )
        })}
      </nav>

      {/* 底部：中性标识。
          这里不声称任何服务状态——原先写死的「服务运行中」与真实后端无关，会误导人；
          要真实在线指示需另接健康检查。 */}
      <div className={styles.footer}>
        <span className={styles.footerText}>nacho 控制台</span>
      </div>
    </aside>
  )
}
