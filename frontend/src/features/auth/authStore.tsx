/**
 * 会话状态：令牌 / 用户资料 / 过期时刻，持久化到 localStorage。
 * Context + useReducer；键名与格式固定，刷新不丢会话。
 */
import {
  createContext,
  useContext,
  useReducer,
  type Dispatch,
  type ReactNode,
} from 'react'
import { SESSION_KEY } from '../../lib/http'
import type { UserProfile } from './authApi'

interface PersistedSession {
  token: string
  user: UserProfile | null
  expiresAt: number
  tokenHash?: string
  deviceName?: string
  remembered?: boolean
}

export interface AuthState {
  token: string
  user: UserProfile | null
  expiresAt: number
  tokenHash?: string
  deviceName?: string
  remembered?: boolean
}

type AuthAction =
  | {
      type: 'SET_SESSION'
      token: string
      user: UserProfile
      expiresInSeconds: number
      tokenHash?: string
      deviceName?: string
      remembered?: boolean
    }
  | { type: 'SET_USER'; user: UserProfile }
  | { type: 'CLEAR' }

function loadSession(): AuthState {
  try {
    const raw = localStorage.getItem(SESSION_KEY)
    if (!raw) return { token: '', user: null, expiresAt: 0 }
    const parsed = JSON.parse(raw) as Partial<PersistedSession>
    if (!parsed.token) return { token: '', user: null, expiresAt: 0 }
    return {
      token: parsed.token,
      user: parsed.user ?? null,
      expiresAt: parsed.expiresAt ?? 0,
      tokenHash: parsed.tokenHash,
      deviceName: parsed.deviceName,
      remembered: parsed.remembered,
    }
  } catch {
    return { token: '', user: null, expiresAt: 0 }
  }
}

function persist(state: AuthState) {
  if (!state.token) {
    localStorage.removeItem(SESSION_KEY)
    return
  }
  const snapshot: PersistedSession = {
    token: state.token,
    user: state.user,
    expiresAt: state.expiresAt,
    tokenHash: state.tokenHash,
    deviceName: state.deviceName,
    remembered: state.remembered,
  }
  localStorage.setItem(SESSION_KEY, JSON.stringify(snapshot))
}

function reducer(state: AuthState, action: AuthAction): AuthState {
  let next: AuthState
  switch (action.type) {
    case 'SET_SESSION':
      next = {
        token: action.token,
        tokenHash: action.tokenHash,
        user: action.user,
        // 0 = 服务端宣告「不过期」（长期会话 / 记住设备），别算成「现在就过期」
        expiresAt:
          action.expiresInSeconds > 0 ? Date.now() + action.expiresInSeconds * 1000 : 0,
        deviceName: action.deviceName,
        remembered: action.remembered,
      }
      break
    case 'SET_USER':
      next = { ...state, user: action.user }
      break
    case 'CLEAR':
      next = { token: '', user: null, expiresAt: 0 }
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
