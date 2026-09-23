/** 登录页：全屏居中的一张高质感卡片（样式自包含）。 */
import { useState, type FormEvent } from 'react'
import { useNavigate, useLocation, Navigate } from 'react-router-dom'
import { login } from './authApi'
import { ApiRequestError } from '../../lib/http'
import { useAuth } from './authStore'
import { ThemeToggle } from '../../common/theme'
import { IconUser, IconLock, IconEye, IconEyeOff, IconAlert } from '../../common/icons'
import {
  SHOW_DEMO_ACCOUNTS,
  DEMO_ACCOUNTS,
  backendUrl,
} from '../../config/env'
import styles from './LoginPage.module.css'

const accountPattern = /^[A-Za-z0-9_.-]{3,32}$/

export default function LoginPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const { dispatch, isAuthenticated } = useAuth()

  const [account, setAccount] = useState('')
  const [password, setPassword] = useState('')
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
      const { data } = await login(account.trim(), password)
      dispatch({
        type: 'SET_SESSION',
        token: data.token,
        user: data.user,
        expiresInSeconds: data.expires_in,
      })
      const from =
        (location.state as { from?: { pathname: string } })?.from?.pathname ?? '/'
      navigate(from, { replace: true })
    } catch (err) {
      if (err instanceof ApiRequestError) {
        const detail = err.details[0]
          ? `${err.details[0].field} · ${err.details[0].message}`
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
          <span className={styles.logo} aria-hidden="true" />
          <span className={styles.brandName}>nacho</span>
          <span className={styles.brandTag}>控制台</span>
        </div>

        <div className={styles.head}>
          <h1 className={styles.title}>欢迎回来</h1>
          <p className={styles.subtitle}>登录以继续使用 nacho 机器人框架控制台</p>
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
          <a href={backendUrl('/docs')} target="_blank" rel="noopener">
            API 文档
          </a>
          <span className={styles.dot}>·</span>
          <span>v0.1.0</span>
        </div>
      </div>
    </div>
  )
}
