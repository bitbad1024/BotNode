/**
 * 工作台：登录后首页。
 * 进入即调 /auth/me 校验会话；令牌失效则清会话回登录页。
 */
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { fetchProfile } from '../auth/authApi'
import { ApiRequestError } from '../../lib/http'
import { useAuth } from '../auth/authStore'
import AvatarImage from '../auth/AvatarImage'
import { useToast } from '../../common/Toast'
import { useNavigate } from 'react-router-dom'
import { NAV_ITEMS } from '../layout/nav'
import {
  IconKey,
  IconLogs,
  IconTerminal,
  IconDevices,
  IconCopy,
  IconExternal,
  IconChevronDown,
} from '../../common/icons'
import { backendUrl } from '../../config/env'
import { Skeleton } from '../../common/Skeleton'
import styles from './DashboardPage.module.css'

const MODULE_ICONS = {
  '/sessions': IconDevices,
  '/bots': IconKey,
  '/logs': IconLogs,
  '/debug': IconTerminal,
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
    // expiresAt === 0：服务端宣告不过期
    if (state.expiresAt === 0) return '长期有效'
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
      <div>
        <div className={styles.skelGreet}>
          <Skeleton width={230} height={26} />
          <Skeleton width={340} height={13} />
        </div>
        <section className={styles.grid}>
          {[0, 1].map((i) => (
            <div key={i} className={`card ${styles.card}`}>
              <Skeleton width={88} height={15} />
              <div className={styles.skelLines} style={{ marginTop: 16 }}>
                <Skeleton width="70%" height={12} />
                <Skeleton width="52%" height={12} />
                <Skeleton width="64%" height={12} />
              </div>
            </div>
          ))}
        </section>
        <section className={styles.modules}>
          <div className={styles.sectionHead}>
            <Skeleton width={92} height={16} />
          </div>
          <div className={styles.moduleGrid}>
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className={`card ${styles.skelModule}`}>
                <Skeleton width={44} height={44} radius={12} />
                <div className={styles.skelLines}>
                  <Skeleton width="58%" height={13} />
                  <Skeleton width="84%" height={11} />
                </div>
              </div>
            ))}
          </div>
        </section>
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
          </div>
          <div className={styles.profileRow}>
            <AvatarImage
              initial={(user.nickname || user.account).slice(0, 1)}
              size="lg"
            />
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
            <span className={styles.cardNote}>
              {state.remembered ? '记住设备 · 30 天' : '会话登录 · 2 小时'}
            </span>
          </div>
          <div className={styles.ttlRow}>
            <span className={styles.ttlValue}>{remaining}</span>
            <div>
              <div className={styles.ttlLabel}>
                {state.expiresAt === 0 ? '会话状态' : '剩余有效期（滑动续期）'}
              </div>
              <div className={styles.ttlHint}>
                {state.expiresAt === 0
                  ? '服务端宣告不过期'
                  : '每次请求自动延后，闲置到期才掉线'}
              </div>
            </div>
          </div>
          <div className={styles.traceRow}>
            <span className={styles.traceLabel}>本机设备名</span>
            <span className={styles.traceId}>{state.deviceName || '未知设备'}</span>
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
                    {m.path === '/sessions' && '查看登录设备，远程下线其他会话'}
                    {m.path === '/bots' && '机器人管理：添加、启停与在线客户端'}
                    {m.path === '/logs' && '运行日志检索'}
                    {m.path === '/debug' && 'OneBot 反向 WS 收发模拟器'}
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
