/**
 * 统一 HTTP 客户端（axios）。
 *
 * 与后端的统一响应壳对齐：
 *   成功 { success:true,  data,            trace_id }
 *   失败 { success:false, error:{code,message,details}, trace_id }
 *
 * - 请求拦截器自动附带 Authorization: Bearer（从 localStorage 读会话）；
 * - 响应拦截器解包，业务失败统一抛 ApiRequestError；
 * - 组件里直接 const { data, traceId } = await get/post(...)。
 */
import axios, { AxiosError, type AxiosResponse } from 'axios'
import { API_BASE_URL } from '../config/env'

/** 会话在 localStorage 的键名（authStore 与拦截器共用）。 */
export const SESSION_KEY = 'nacho.console.session'

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

const instance = axios.create({
  baseURL: API_BASE_URL,
  timeout: 15000,
})

// 请求：附带令牌
instance.interceptors.request.use((config) => {
  try {
    const raw = localStorage.getItem(SESSION_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as { token?: string }
      if (parsed.token) {
        config.headers.Authorization = `Bearer ${parsed.token}`
      }
    }
  } catch {
    /* 会话不可读时忽略，按匿名请求发出 */
  }
  return config
})

// 响应：解包统一外壳
instance.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    if (error.response) {
      const { status, data } = error.response
      const body = (data ?? {}) as {
        error?: ErrorEnvelope
        trace_id?: string
      }
      const envelope = body.error ?? {}
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
}

async function request<T>(p: Promise<AxiosResponse>): Promise<ApiOk<T>> {
  const res = await p
  return { data: res.data.data as T, traceId: res.data.trace_id ?? '-' }
}

export const http = {
  get: <T>(url: string) => request<T>(instance.get(url)),
  post: <T, B = unknown>(url: string, body?: B) =>
    request<T>(instance.post(url, body)),
}
