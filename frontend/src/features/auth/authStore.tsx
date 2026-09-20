/**
 * 会话状态：令牌 / 用户资料 / 过期时刻 / 设备摘要，Context + useReducer。
 *
 * 持久化跟着「记住设备」走两份存储：
 * - 勾了：localStorage，关掉浏览器还在（服务端滑动有效期默认 30 天）；
 * - 没勾：sessionStorage，标签页一关即丢（与会话 Cookie 同寿，默认 2 小时闲置）。
 *
 * 另外订阅 http 层两个全局事件：
 * - 会话心跳（X-Session-Expires-In）→ 拨准剩余有效期（滑动续期）；
 * - 任意 401 → 清会话，路由守卫会自动把用户踢回登录页。
 */
import {
  createContext,
  useContext,
  useEffect,
  useReducer,
  type Dispatch,
  type ReactNode,
} from 'react'
import {
  SESSION_KEY,
  SESSION_TICK_EVENT,
  UNAUTHORIZED_EVENT,
} from '../../lib/http'
import type { UserProfile } from './authApi'

interface PersistedSession {
  token: string
  tokenHash: string
  user: UserProfile | null
  expiresAt: number
  /** 0 表示服务端宣告「不过期」 */
  expiresIn: number
  remembered: boolean
  deviceName: string
}

export interface AuthState {
  token: string
  tokenHash: string
  user: UserProfile | null
  /** 本地按滑动续期维护的到期时刻（epoch ms）；0 = 不过期 */
  expiresAt: number
  remembered: boolean
  deviceName: string
}

type AuthAction =
  | {
      type: 'SET_SESSION'
      token: string
      tokenHash: string
      user: UserProfile
      /** 登录那一刻的剩余秒数 */
      expiresInSeconds: number
      remembered: boolean
      deviceName: string
    }
  | { type: 'SET_USER'; user: UserProfile }
  | { type: 'TOUCH'; expiresInSeconds: number }
  | { type: 'CLEAR' }

const EMPTY: AuthState = {
  token: '',
  tokenHash: '',
  user: null,
  expiresAt: 0,
  remembered: false,
  deviceName: '',
}

function readPersisted(): PersistedSession | null {
  try {
    for (const storage of [localStorage, sessionStorage]) {
      const raw = storage.getItem(SESSION_KEY)
      if (!raw) continue
      const parsed = JSON.parse(raw) as Partial<PersistedSession>
      if (parsed.token) return parsed as PersistedSession
    }
  } catch {
    /* 存储不可读按未登录处理 */
  }
  return null
}

function loadSession(): AuthState {
  const parsed = readPersisted()
  if (!parsed) return EMPTY
  return {
    token: parsed.token,
    tokenHash: parsed.tokenHash ?? '',
    user: parsed.user ?? null,
    expiresAt: parsed.expiresAt ?? 0,
    remembered: parsed.remembered ?? false,
    deviceName: parsed.deviceName ?? '',
  }
}

function persist(state: AuthState) {
  // 无论写哪份，另一份里的旧会话都要抹掉，避免换设备/重新登录后读到陈旧快照
  localStorage.removeItem(SESSION_KEY)
  sessionStorage.removeItem(SESSION_KEY)
  if (!state.token) return
  const snapshot: PersistedSession = {
    token: state.token,
    tokenHash: state.tokenHash,
    user: state.user,
    expiresAt: state.expiresAt,
    expiresIn: state.expiresAt === 0 ? 0 : Math.round((state.expiresAt - Date.now()) / 1000),
    remembered: state.remembered,
    deviceName: state.deviceName,
  }
  ;(state.remembered ? localStorage : sessionStorage).setItem(
    SESSION_KEY,
    JSON.stringify(snapshot),
  )
}

function reducer(state: AuthState, action: AuthAction): AuthState {
  let next: AuthState
  switch (action.type) {
    case 'SET_SESSION':
      next = {
        token: action.token,
        tokenHash: action.tokenHash,
        user: action.user,
        expiresAt:
          action.expiresInSeconds > 0
            ? Date.now() + action.expiresInSeconds * 1000
            : 0,
        remembered: action.remembered,
        deviceName: action.deviceName,
      }
      break
    case 'SET_USER':
      next = { ...state, user: action.user }
      break
    case 'TOUCH':
      // 滑动续期：只拨到期时刻，不动别的；0 = 不过期
      next = {
        ...state,
        expiresAt:
          action.expiresInSeconds > 0
            ? Date.now() + action.expiresInSeconds * 1000
            : 0,
      }
      break
    case 'CLEAR':
      next = EMPTY
      break
    default:
      return state
  }
  persist(next)
  return next
}

interface AuthContextValue {
  state: AuthState
  dispatch: Dispatch<AuthAction>
  isAuthenticated: boolean
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, undefined, loadSession)

  useEffect(() => {
    const onTick = (e: Event) => {
      const secs = (e as CustomEvent<number>).detail
      if (typeof secs === 'number') {
        dispatch({ type: 'TOUCH', expiresInSeconds: secs })
      }
    }
    const onUnauthorized = () => dispatch({ type: 'CLEAR' })
    window.addEventListener(SESSION_TICK_EVENT, onTick)
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    return () => {
      window.removeEventListener(SESSION_TICK_EVENT, onTick)
      window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    }
  }, [])

  return (
    <AuthContext.Provider
      value={{ state, dispatch, isAuthenticated: Boolean(state.token) }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用')
  return ctx
}
