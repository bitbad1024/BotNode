/** 注册页：与登录页同一张卡（样式直接复用 LoginPage.module.css）。 */
import { useState, type FormEvent } from 'react'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { register } from './authApi'
import { ApiRequestError } from '../../lib/http'
import { useAuth } from './authStore'
import { ThemeToggle } from '../../common/theme'
import {
  IconUser,
  IconLock,
  IconEye,
  IconEyeOff,
  IconAlert,
  IconEdit,
  IconLogo,
} from '../../common/icons'
import { backendUrl } from '../../config/env'
import {
  FIELD_LABELS,
  NICKNAME_MAX_LENGTH,
  NICKNAME_MIN_LENGTH,
  PASSWORD_MAX_LENGTH,
  PASSWORD_MIN_LENGTH,
  accountPattern,
} from './formRules'
import styles from './LoginPage.module.css'

export default function RegisterPage() {
  const navigate = useNavigate()
  const { isAuthenticated } = useAuth()

  const [account, setAccount] = useState('')
  const [password, setPassword] = useState('')
  const [nickname, setNickname] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading] = useState(false)
  const [touchedA, setTouchedA] = useState(false)
  const [touchedP, setTouchedP] = useState(false)
  const [touchedN, setTouchedN] = useState(false)
  /** 后端说这个账号已经有人了：输入框一并标红，改账号时清掉 */
  const [accountTaken, setAccountTaken] = useState(false)
  const [error, setError] = useState<{
    title: string
    detail: string
    traceId: string
  } | null>(null)

  if (isAuthenticated) return <Navigate to="/" replace />

  const accountInvalid =
    (touchedA && account.length > 0 && !accountPattern.test(account)) || accountTaken
  const passwordInvalid =
    touchedP &&
    password.length > 0 &&
    (password.length < PASSWORD_MIN_LENGTH || password.length > PASSWORD_MAX_LENGTH)
  const nicknameInvalid =
    touchedN &&
    (nickname.trim().length < NICKNAME_MIN_LENGTH ||
      nickname.trim().length > NICKNAME_MAX_LENGTH)
  const canSubmit =
    !loading &&
    accountPattern.test(account) &&
    password.length >= PASSWORD_MIN_LENGTH &&
    password.length <= PASSWORD_MAX_LENGTH &&
    nickname.trim().length >= NICKNAME_MIN_LENGTH &&
    nickname.trim().length <= NICKNAME_MAX_LENGTH

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setTouchedA(true)
    setTouchedP(true)
    setTouchedN(true)
    if (!canSubmit) return
    setLoading(true)
    setError(null)
    try {
      const { data } = await register(account.trim(), password, nickname.trim())
      // 注册不返回令牌：带着账号去登录页预填，让用户自己登一次
      navigate('/login', { state: { account: data.account, justRegistered: true } })
    } catch (err) {
      if (err instanceof ApiRequestError) {
        const d = err.details[0]
        const detail = d
          ? `${FIELD_LABELS[d.field] ?? d.field}：${d.message}`
          : err.message
        setError({ title: err.message, detail, traceId: err.traceId })
        if (err.code === 'ACCOUNT_ALREADY_EXISTS') setAccountTaken(true)
      } else {
        setError({ title: '注册失败', detail: '未知错误，请稍后再试', traceId: '-' })
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
          <span className={styles.brandName}>BotNode</span>
          <span className={styles.brandTag}>控制台</span>
        </div>

        <div className={styles.head}>
          <h1 className={styles.title}>创建账号</h1>
          <p className={styles.subtitle}>注册后用同样的账号密码登录即可</p>
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
            <label className={styles.label} htmlFor="register-account">
              账号
            </label>
            <div className={`${styles.control} ${accountInvalid ? styles.invalid : ''}`}>
              <span className={styles.controlIcon}>
                <IconUser size={18} />
              </span>
              <input
                id="register-account"
                type="text"
                autoComplete="username"
                placeholder="3-32 位字母、数字、下划线、点、短横线"
                value={account}
                onChange={(e) => {
                  setAccount(e.target.value)
                  setAccountTaken(false)
                }}
                onBlur={() => setTouchedA(true)}
              />
            </div>
          </div>

          <div className={styles.field}>
            <label className={styles.label} htmlFor="register-password">
              密码
            </label>
            <div className={`${styles.control} ${passwordInvalid ? styles.invalid : ''}`}>
              <span className={styles.controlIcon}>
                <IconLock size={18} />
              </span>
              <input
                id="register-password"
                type={showPassword ? 'text' : 'password'}
                autoComplete="new-password"
                placeholder={`${PASSWORD_MIN_LENGTH}-${PASSWORD_MAX_LENGTH} 位`}
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

          <div className={styles.field}>
            <label className={styles.label} htmlFor="register-nickname">
              昵称
            </label>
            <div className={`${styles.control} ${nicknameInvalid ? styles.invalid : ''}`}>
              <span className={styles.controlIcon}>
                <IconEdit size={18} />
              </span>
              <input
                id="register-nickname"
                type="text"
                autoComplete="nickname"
                placeholder={`展示用，最多 ${NICKNAME_MAX_LENGTH} 个字符`}
                value={nickname}
                onChange={(e) => setNickname(e.target.value)}
                onBlur={() => setTouchedN(true)}
              />
            </div>
          </div>

          <button className={styles.submit} type="submit" disabled={!canSubmit}>
            {loading ? (
              <span className={styles.submitInner}>
                <span className="spinner" />
                正在注册…
              </span>
            ) : (
              '注 册'
            )}
          </button>
        </form>

        <div className={styles.alt}>
          已经有账号了？<Link to="/login">去登录</Link>
        </div>

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
