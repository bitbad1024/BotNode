/**
 * 运行日志 API：GET /api/logs。
 *
 * 与后端契约一一对应（见 nacho/api/api/log/router.py）：
 * - 响应只有数组、没有 total：是否还有下一页靠「返回条数 == limit」推断；
 * - 结果按时间倒序，limit 1-500，offset 翻页；
 * - 非管理员后端强制只看自己的 owner_id，指定别人会 403，processors 也只有管理员能传；
 * - owner_id 传空串是「只看公共日志」，所以空串不能像别的参数一样丢掉；
 * - 默认只查落库出口（database），库出口没开后端回 503。
 */
import { http } from '../../lib/http'

/** 后端日志级别（LogLevel 枚举名，严格大写）。 */
export const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] as const
export type LogLevelName = (typeof LOG_LEVELS)[number]

/** 一条运行日志：字段原样来自后端，不做二次拼串。 */
export interface LogEntry {
  /** 记录编号（同一批去重、定位单条用） */
  record_id: string
  /** Unix 时间戳（秒） */
  timestamp: number
  level: LogLevelName | string
  /** 写日志的实例名，如 nacho.api */
  logger_name: string
  /** 归属者；空串 = 公共（框架自身的日志） */
  owner_id: string
  message: string
  /** 结构化附加字段，如 request_id / robot_id */
  extra: Record<string, unknown>
  /** 异常栈文本；没有异常为 null */
  exc_text: string | null
}

/** 检索参数：全部可选；字段名即后端 query 参数名。 */
export interface LogSearchParams {
  level?: string
  /** 模块名：后端是精确匹配 */
  logger_name?: string
  /** 正文模糊匹配 */
  query?: string
  /** 归属者精确匹配；空串 = 只看公共日志（有语义，不能省略成 undefined） */
  owner_id?: string
  /** Unix 时间戳（秒）或 ISO 字符串，闭区间 */
  start?: string
  end?: string
  limit?: number
  offset?: number
  /** 出口名，逗号分隔；仅管理员，默认 database */
  processors?: string
}

/** 检索日志。params 里 undefined 的字段不发，空串照发（owner_id='' 有语义）。 */
export function searchLogs(params: LogSearchParams = {}) {
  return http.get<LogEntry[]>('/logs', { params })
}
