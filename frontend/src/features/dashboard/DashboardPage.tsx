/**
 * 工作台：登录后首页。
 * 进入即调 /auth/me 校验会话；令牌失效则清会话回登录页。
 */
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { fetchProfile } from '../auth/authApi'
import { ApiRequestError } from '../../lib/http'
import { useAuth } from '../auth/authStore'
import { useToast } from '../../common/Toast'
import { useNavigate } from 'react-router-dom'
import { NAV_ITEMS } from '../layout/nav'
import {
  IconRobot,
  IconSchedule,
  IconLogs,
  IconCopy,
  IconExternal,
  IconChevronDown,
} from '../../common/icons'
import { backendUrl } from '../../config/env'
import styles from './DashboardPage.module.css'

const MODULE_ICONS = {
  '/robots': IconRobot,
  '/schedule': IconSchedule,
  '/logs': IconLogs,
} as const

export default function DashboardPage() {
  const navigate = useNavigate()
  const { state, dispatch } = useAuth()
  const { pushToast } = useToast()

  const [loading, setLoading] = useState(true)
  const [traceId, setTraceId] = useState('—')
  const [now, setNow] = useState(Date.now())

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const { data, traceId: tid } = await fetchProfile()
        if (cancelled) return
        dispatch({ type: 'SET_USER', user: data })
        setTraceId(tid)
      } catch (err) {
        if (cancelled) return
        if (
          err instanceof ApiRequestError &&
          (err.status === 401 || err.code === 'TOKEN_EXPIRED')
        ) {
          dispatch({ type: 'CLEAR' })
          pushToast('info', '登录已过期，请重新登录')
          navigate('/login', { replace: true })
          return
        }
        pushToast(
          'error',
          err instanceof ApiRequestError ? err.message : '会话校验失败',
        )
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [dispatch, pushToast, navigate])

  const remaining = useMemo(() => {
    const secs = Math.max(0, Math.floor((state.expiresAt - now) / 1000))
    return `${String(Math.floor(secs / 60)).padStart(2, '0')}:${String(
      secs % 60,
    ).padStart(2, '0')}`
  }, [state.expiresAt, now])

  const maskedToken = useMemo(() => {
    const t = state.token
    return t.length <= 20 ? t : `${t.slice(0, 14)}······${t.slice(-6)}`
  }, [state.token])

  async function copyToken() {
    try {
      await navigator.clipboard.writeText(state.token)
      pushToast('success', '令牌已复制到剪贴板')
    } catch {
      const ta = document.createElement('textarea')
      ta.value = state.token
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      pushToast('success', '令牌已复制到剪贴板')
    }
  }

  const user = state.user
  const modules = NAV_ITEMS.filter((n) => n.path !== '/')

  if (loading || !user) {
    return (
      <div className={styles.loading}>
        <span className="spinner" />
        <span>正在加载工作台…</span>
      </div>
    )
  }

  return (
    <div className="rise">
      {/* 欢迎 */}
      <header className={styles.greet}>
        <h2 className={styles.greetTitle}>
          你好，{user.nickname || user.account}
        </h2>
        <p className={styles.greetSub}>
          欢迎使用 nacho 机器人框架控制台，令牌有效，接口层随时听调。
        </p>
      </header>

      {/* 概览卡 */}
      <section className={styles.grid}>
        <div className={`card ${styles.card}`}>
          <div className={styles.cardHead}>
            <h3 className={styles.cardTitle}>用户资料</h3>
            <span className={styles.cardNote}>GET /api/auth/me</span>
          </div>
          <div className={styles.profileRow}>
            <span className={styles.avatar}>
              {(user.nickname || user.account).slice(0, 1)}
            </span>
            <div>
              <div className={styles.profileNick}>{user.nickname || '—'}</div>
              <div className={styles.profileAccount}>@{user.account}</div>
            </div>
          </div>
          <dl className={styles.dataList}>
            <div className={styles.dataRow}>
              <dt>用户 ID</dt>
              <dd className={styles.mono}>{user.id}</dd>
            </div>
            <div className={styles.dataRow}>
              <dt>角色</dt>
              <dd className={styles.roles}>
                {user.roles.map((r) => (
                  <span key={r} className={`chip ${r === 'admin' ? 'chip-accent' : ''}`}>
                    {r}
                  </span>
                ))}
              </dd>
            </div>
          </dl>
        </div>

        <div className={`card ${styles.card}`}>
          <div className={styles.cardHead}>
            <h3 className={styles.cardTitle}>当前会话</h3>
            <span className={styles.cardNote}>Bearer Token</span>
          </div>
          <div className={styles.ttlRow}>
            <span className={styles.ttlValue}>{remaining}</span>
            <div>
              <div className={styles.ttlLabel}>令牌剩余有效期</div>
              <div className={styles.ttlHint}>到期后请重新登录</div>
            </div>
          </div>
          <div className={styles.tokenRow}>
            <span className={`${styles.tokenText} ${styles.mono}`}>{maskedToken}</span>
            <button className={`btn ${styles.copyBtn}`} onClick={copyToken}>
              <IconCopy size={15} />
              复制
            </button>
          </div>
          <div className={styles.traceRow}>
            <span className={styles.traceLabel}>最近请求 trace_id</span>
            <span className={`${styles.mono} ${styles.traceId}`}>{traceId}</span>
          </div>
        </div>
      </section>

      {/* 模块入口 */}
      <section className={styles.modules}>
        <div className={styles.sectionHead}>
          <h3 className={styles.sectionTitle}>功能模块</h3>
          <a
            className={styles.docLink}
            href={backendUrl('/docs')}
            target="_blank"
            rel="noopener"
          >
            接口文档 <IconExternal size={13} />
          </a>
        </div>
        <div className={styles.moduleGrid}>
          {modules.map((m) => {
            const Icon = MODULE_ICONS[m.path as keyof typeof MODULE_ICONS] ?? m.icon
            return (
              <Link key={m.path} to={m.path} className={`card ${styles.moduleCard}`}>
                <span className={styles.moduleIcon}>
                  <Icon size={22} />
                </span>
                <div className={styles.moduleText}>
                  <span className={styles.moduleName}>{m.label}</span>
                  <span className={styles.moduleHint}>
                    {m.path === '/robots' && '机器人在线状态与任务'}
                    {m.path === '/schedule' && 'cron 定时任务编排'}
                    {m.path === '/logs' && '运行日志检索'}
                  </span>
                </div>
                <IconChevronDown size={17} className={styles.moduleArrow} />
              </Link>
            )
          })}
        </div>
      </section>
    </div>
  )
}
