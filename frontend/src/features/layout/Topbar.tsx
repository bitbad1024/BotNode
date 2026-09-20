/**
 * 顶栏：
 * - 左侧：小屏汉堡（开抽屉）/ 桌面折叠按钮（收侧栏）+ 当前页面标题；
 * - 右侧：主题切换 + 用户菜单（资料、退出登录）。
 */
import { useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { NAV_ITEMS } from './nav'
import { useLayout } from './layoutStore'
import { useAuth } from '../auth/authStore'
import { revokeSession } from '../auth/authApi'
import { ThemeToggle } from '../../common/theme'
import {
  IconMenu,
  IconCollapse,
  IconExpand,
  IconChevronDown,
  IconLogout,
  IconDevices,
} from '../../common/icons'
import styles from './Topbar.module.css'

export default function Topbar() {
  const location = useLocation()
  const navigate = useNavigate()
  const { state, dispatch } = useAuth()
  const { collapsed, toggleCollapsed, toggleMobile } = useLayout()
  const [menuOpen, setMenuOpen] = useState(false)
  const [loggingOut, setLoggingOut] = useState(false)

  const current =
    NAV_ITEMS.find((i) =>
      i.end ? location.pathname === i.path : location.pathname.startsWith(i.path),
    ) ?? NAV_ITEMS[0]

  const user = state.user
  const initial = (user?.nickname || user?.account || '?').slice(0, 1)

  async function logout() {
    // 调吊销接口让服务端会话失效（会顺带清 HttpOnly Cookie）；接口失败也照常本地清退，
    // 避免「网络抖一下就退不出去」。
    setMenuOpen(false)
    setLoggingOut(true)
    try {
      if (state.tokenHash) {
        await revokeSession(state.tokenHash)
      }
    } catch {
      /* 服务端没吊销成功（已过期 / 网络问题）也不拦本地退出 */
    } finally {
      setLoggingOut(false)
      dispatch({ type: 'CLEAR' })
      navigate('/login', { replace: true })
    }
  }

  return (
    <header className={styles.topbar}>
      <div className={styles.left}>
        {/* 小屏汉堡 */}
        <button
          className={`icon-btn ${styles.menuBtn}`}
          onClick={toggleMobile}
          aria-label="打开导航"
        >
          <IconMenu size={20} />
        </button>
        {/* 桌面折叠 */}
        <button
          className={`icon-btn ${styles.collapseBtn}`}
          onClick={toggleCollapsed}
          aria-label={collapsed ? '展开侧边栏' : '折叠侧边栏'}
          title={collapsed ? '展开侧边栏' : '折叠侧边栏'}
        >
          {collapsed ? <IconExpand size={19} /> : <IconCollapse size={19} />}
        </button>

        <h1 className={styles.title}>{current.label}</h1>
      </div>

      <div className={styles.right}>
        <ThemeToggle />

        <div className={styles.userWrap}>
          <button
            className={styles.userBtn}
            onClick={() => setMenuOpen((v) => !v)}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
          >
            <span className={styles.avatar}>{initial}</span>
            <span className={styles.userMeta}>
              <span className={styles.userName}>
                {user?.nickname || user?.account}
              </span>
            </span>
            <IconChevronDown size={15} className={styles.caret} />
          </button>

          {menuOpen && (
            <>
              <div
                className={styles.menuBackdrop}
                onClick={() => setMenuOpen(false)}
              />
              <div className={styles.menu} role="menu">
                <div className={styles.menuHead}>
                  <span className={styles.menuAvatar}>{initial}</span>
                  <div className={styles.menuUser}>
                    <span className={styles.menuName}>
                      {user?.nickname || '未命名'}
                    </span>
                    <span className={styles.menuAccount}>@{user?.account}</span>
                  </div>
                </div>
                {user && user.roles.length > 0 && (
                  <div className={styles.menuRoles}>
                    {user.roles.map((r) => (
                      <span key={r} className={`chip ${r === 'admin' ? 'chip-accent' : ''}`}>
                        {r}
                      </span>
                    ))}
                  </div>
                )}
                <div className={styles.menuDivider} />
                <Link
                  to="/sessions"
                  className={styles.menuItem}
                  role="menuitem"
                  onClick={() => setMenuOpen(false)}
                >
                  <IconDevices size={17} />
                  登录设备
                </Link>
                <button
                  className={styles.logoutBtn}
                  onClick={() => void logout()}
                  role="menuitem"
                  disabled={loggingOut}
                >
                  <IconLogout size={17} />
                  {loggingOut ? '正在退出…' : '退出登录'}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </header>
  )
}
