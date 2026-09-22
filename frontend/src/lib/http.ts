/**
 * 统一 HTTP 客户端（axios）。
 *
 * 与后端的统一响应壳对齐：
 *   成功 { success:true,  data,            trace_id }
 *   失败 { success:false, error:{code,message,details}, trace_id }
 *
 * 认证是**双轨**的（后端 cookie 路径限定在 /api/auth）：
 * - withCredentials 让浏览器自动收发 HttpOnly Cookie（nacho_session），/api/auth/* 靠它；
 * - 同时从本地存储读会话令牌附带 Authorization: Bearer —— /api/onebot/* 不在 cookie
 *   路径内，只能靠这个头。
 *
 * 另外两件全局事：
 * - 每个认证成功的响应都带 X-Session-Expires-In（滑动续期后的剩余秒数），这里广播
 *   「会话心跳」事件，authStore 收到后拨准倒计时；
 * - 任何请求回 401 都广播「未授权」事件，authStore 收到清会话，路由守卫自然踢回登录页。
 */
import axios, {
  AxiosError,
  type AxiosRequestConfig,
  type AxiosResponse,
} from 'axios'
import { API_BASE_URL } from '../config/env'

/** 会话在浏览器存储里的键名（localStorage / sessionStorage 同名）。 */
export const SESSION_KEY = 'nacho.console.session'

/** 滑动续期心跳：detail 是剩余秒数（0 = 不过期）。 */
export const SESSION_TICK_EVENT = 'nacho:session-tick'
/** 任意请求 401：会话已失效。 */
export const UNAUTHORIZED_EVENT = 'nacho:unauthorized'

export interface ErrorDetail {
  field: string
  message: string
}

/** 业务 / HTTP 错误的统一形态。 */
export class ApiRequestError extends Error {
  readonly status: number
  readonly code: string
  readonly details: ErrorDetail[]
  readonly traceId: string

  constructor(
    message: string,
    params: {
      status?: number
      code?: string
      details?: ErrorDetail[]
      traceId?: string
    } = {},
  ) {
    super(message)
    this.name = 'ApiRequestError'
    this.status = params.status ?? 0
    this.code = params.code ?? 'UNKNOWN'
    this.details = params.details ?? []
    this.traceId = params.traceId ?? '-'
  }
}

interface ErrorEnvelope {
  code?: string
  message?: string
  details?: ErrorDetail[]
}

/** 读会话令牌：勾了「记住设备」在 localStorage，没勾在 sessionStorage（关浏览器即丢）。 */
export function readStoredToken(): string {
  try {
    for (const storage of [localStorage, sessionStorage]) {
      const raw = storage.getItem(SESSION_KEY)
      if (raw) {
        const parsed = JSON.parse(raw) as { token?: string }
        if (parsed.token) return parsed.token
      }
    }
  } catch {
    /* 存储不可读时按匿名处理 */
  }
  return ''
}

const instance = axios.create({
  baseURL: API_BASE_URL,
  timeout: 15000,
  // HttpOnly Cookie 靠它随 /api/auth/* 请求自动带上
  withCredentials: true,
})

// 请求：附带 Bearer 令牌（/api/onebot/* 不在 cookie 路径内，必须带头）
instance.interceptors.request.use((config) => {
  const token = readStoredToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应：成功时广播滑动续期；失败解包统一外壳，401 额外广播失效事件
instance.interceptors.response.use(
  (response) => {
    const remains = response.headers?.['x-session-expires-in']
    if (remains !== undefined && remains !== null && remains !== '') {
      const secs = Number(remains)
      if (Number.isFinite(secs)) {
        window.dispatchEvent(new CustomEvent(SESSION_TICK_EVENT, { detail: secs }))
      }
    }
    return response
  },
  (error: AxiosError) => {
    if (error.response) {
      const { status, data } = error.response
      const body = (data ?? {}) as {
        error?: ErrorEnvelope
        trace_id?: string
      }
      const envelope = body.error ?? {}
      if (status === 401) {
        // 登录接口自己的 401（账号密码错）也会走到这：authStore 收到只做幂等清会话，
        // 登录页自己 catch 展示错误，互不干扰。
        window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT))
      }
      throw new ApiRequestError(envelope.message || `请求失败（${status}）`, {
        status,
        code: envelope.code,
        details: envelope.details,
        traceId: body.trace_id,
      })
    }
    if (error.request) {
      throw new ApiRequestError('无法连接服务器，请检查后端是否启动', {
        code: 'NETWORK_ERROR',
      })
    }
    throw new ApiRequestError(error.message || '未知请求错误', {
      code: 'REQUEST_SETUP',
    })
  },
)

export interface ApiOk<T> {
  data: T
  traceId: string
  /** 滑动续期后的会话剩余秒数（仅认证接口的响应带；0 = 不过期）。 */
  expiresIn: number | null
}

async function request<T>(p: Promise<AxiosResponse>): Promise<ApiOk<T>> {
  const res = await p
  const rawExpires = res.headers?.['x-session-expires-in']
  const expiresIn =
    rawExpires !== undefined && rawExpires !== null && rawExpires !== ''
      ? Number(rawExpires)
      : null
  return {
    data: res.data.data as T,
    traceId: res.data.trace_id ?? '-',
    expiresIn: Number.isFinite(expiresIn as number) ? expiresIn : null,
  }
}

export const http = {
  /** GET：第二个参数透传 axios 配置，查询条件走 config.params（自动 URL 编码）。 */
  get: <T>(url: string, config?: AxiosRequestConfig) =>
    request<T>(instance.get(url, config)),
  post: <T, B = unknown>(url: string, body?: B) =>
    request<T>(instance.post(url, body)),
  /** DELETE：令牌吊销、踢下线这类动作用；查询串直接拼在 url 上。 */
  del: <T>(url: string) => request<T>(instance.delete(url)),
  /** PATCH：改单个字段（如令牌启用 / 停用）用。 */
  patch: <T, B = unknown>(url: string, body?: B) =>
    request<T>(instance.patch(url, body)),
}
