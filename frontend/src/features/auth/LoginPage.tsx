/** 登录页：全屏居中的一张高质感卡片（样式自包含）。 */
import { useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate, useLocation, Navigate } from 'react-router-dom'
import { login } from './authApi'
import { ApiRequestError } from '../../lib/http'
import { useAuth } from './authStore'
import { useToast } from '../../common/Toast'
import { ThemeToggle } from '../../common/theme'
import { IconUser, IconLock, IconEye, IconEyeOff, IconAlert, IconCheck, IconLogo } from '../../common/icons'
import { SHOW_DEMO_ACCOUNTS, DEMO_ACCOUNTS } from '../../config/env'
import { FIELD_LABELS, accountPattern } from './formRules'
import styles from './LoginPage.module.css'

export default function LoginPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const { dispatch, isAuthenticated } = useAuth()
  const { pushToast } = useToast()

  /** 从注册页跳过来时带着账号（justRegistered 只为了提示一句） */
  const justRegistered =
    (location.state as { justRegistered?: boolean })?.justRegistered ?? false
  useEffect(() => {
    if (justRegistered) pushToast('success', '注册成功，请用刚注册的账号登录')
  }, [justRegistered, pushToast])

  const [account, setAccount] = useState(
    (location.state as { account?: string })?.account ?? '',
  )
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading] = useState(false)
  const [touchedA, setTouchedA] = useState(false)
  const [touchedP, setTouchedP] = useState(false)
  const [error, setError] = useState<{
    title: string
    detail: string
    traceId: string
  } | null>(null)

  if (isAuthenticated) return <Navigate to="/" replace />

  const accountInvalid = touchedA && account.length > 0 && !accountPattern.test(account)
  const passwordInvalid =
    touchedP && password.length > 0 && (password.length < 8 || password.length > 128)
  const canSubmit = !loading && accountPattern.test(account) && password.length >= 8

  function fillDemo(a: string, p: string) {
    setAccount(a)
    setPassword(p)
    setTouchedA(true)
    setTouchedP(true)
    setError(null)
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setTouchedA(true)
    setTouchedP(true)
    if (!accountPattern.test(account) || password.length < 8) return
    setLoading(true)
    setError(null)
    try {
      const { data } = await login(account.trim(), password, remember)
      dispatch({
        type: 'SET_SESSION',
        token: data.token,
        tokenHash: data.token_hash,
        user: data.user,
        expiresInSeconds: data.expires_in,
        remembered: remember,
        deviceName: data.device_name,
      })
      if (data.reused) {
        pushToast('info', '已复用这台设备上的现有登录')
      } else if (data.device_name) {
        pushToast('success', `已在新设备登录：${data.device_name}`)
      }
      const from =
        (location.state as { from?: { pathname: string } })?.from?.pathname ?? '/'
      navigate(from, { replace: true })
    } catch (err) {
      if (err instanceof ApiRequestError) {
        const d = err.details[0]
        const detail = d
          ? `${FIELD_LABELS[d.field] ?? d.field}：${d.message}`
          : err.message
        setError({ title: err.message, detail, traceId: err.traceId })
      } else {
        setError({ title: '登录失败', detail: '未知错误，请稍后再试', traceId: '-' })
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className={styles.page}>
      <div className={styles.themeCorner}>
        <ThemeToggle />
      </div>

      <div className={`${styles.card} rise`}>
        <div className={styles.brand}>
          <span className={styles.logo} aria-hidden="true">
            <IconLogo size={18} />
          </span>
          <span className={styles.brandName}>TickNeko</span>
          <span className={styles.brandTag}>控制台</span>
        </div>

        <div className={styles.head}>
          <h1 className={styles.title}>欢迎回来</h1>
          <p className={styles.subtitle}>登录以继续使用 TickNeko 机器人框架控制台</p>
        </div>

        <form className={styles.form} onSubmit={onSubmit} noValidate>
          {error && (
            <div className={styles.errorBar} role="alert">
              <span className={styles.errorIcon}>
                <IconAlert size={18} />
              </span>
              <div>
                <div className={styles.errorTitle}>{error.title}</div>
                {error.detail !== error.title && (
                  <div className={styles.errorDetail}>{error.detail}</div>
                )}
                {error.traceId !== '-' && (
                  <div className={styles.errorTrace}>trace · {error.traceId}</div>
                )}
              </div>
            </div>
          )}

          <div className={styles.field}>
            <label className={styles.label} htmlFor="login-account">
              账号
            </label>
            <div className={`${styles.control} ${accountInvalid ? styles.invalid : ''}`}>
              <span className={styles.controlIcon}>
                <IconUser size={18} />
              </span>
              <input
                id="login-account"
                type="text"
                autoComplete="username"
                placeholder="请输入账号"
                value={account}
                onChange={(e) => setAccount(e.target.value)}
                onBlur={() => setTouchedA(true)}
              />
            </div>
          </div>

          <div className={styles.field}>
            <label className={styles.label} htmlFor="login-password">
              密码
            </label>
            <div className={`${styles.control} ${passwordInvalid ? styles.invalid : ''}`}>
              <span className={styles.controlIcon}>
                <IconLock size={18} />
              </span>
              <input
                id="login-password"
                type={showPassword ? 'text' : 'password'}
                autoComplete="current-password"
                placeholder="请输入密码"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                onBlur={() => setTouchedP(true)}
              />
              <button
                type="button"
                className={styles.eye}
                aria-label={showPassword ? '隐藏密码' : '显示密码'}
                onClick={() => setShowPassword(!showPassword)}
              >
                {showPassword ? <IconEyeOff size={18} /> : <IconEye size={18} />}
              </button>
            </div>
          </div>

          <label className={styles.rememberRow}>
            <input
              type="checkbox"
              className={styles.rememberBox}
              checked={remember}
              onChange={(e) => setRemember(e.target.checked)}
            />
            <span className={styles.rememberMark} aria-hidden="true">
              <IconCheck size={12} />
            </span>
            <span className={styles.rememberText}>
              记住这台设备
              <i>30 天免登录；不勾则关闭浏览器即失效（2 小时）</i>
            </span>
          </label>

          <button className={styles.submit} type="submit" disabled={!canSubmit}>
            {loading ? (
              <span className={styles.submitInner}>
                <span className="spinner" />
                正在验证…
              </span>
            ) : (
              '登 录'
            )}
          </button>
        </form>

        <div className={styles.alt}>
          还没有账号？<Link to="/register">注册一个</Link>
        </div>

        {SHOW_DEMO_ACCOUNTS && DEMO_ACCOUNTS.length > 0 && (
          <div className={styles.demo}>
            <div className={styles.demoLabel}>演示账号 · 点击填充</div>
            <div className={styles.demoList}>
              {DEMO_ACCOUNTS.map((d) => (
                <button
                  key={d.account}
                  type="button"
                  className={styles.demoChip}
                  onClick={() => fillDemo(d.account, d.password)}
                >
                  <b>{d.account}</b>
                  <i>{d.password}</i>
                </button>
              ))}
            </div>
          </div>
        )}

        <div className={styles.footer}>
          <span>v0.1.0</span>
        </div>
      </div>
    </div>
  )
}
