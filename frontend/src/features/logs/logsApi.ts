/**
 * 运行日志 API：GET /api/logs。
 *
 * 与后端契约一一对应（见 nacho/api/api/log/router.py）：
 * - 响应是一页：`{ items, total }`，total 是命中总数，前端据此算总页数、做页码跳转；
 * - 结果按自增序号倒序（即写入顺序倒序；同一毫秒的几条也有先后），limit 1-500，offset 翻页；
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
  /** 自增序号：落库那份的写入顺序，本页就是按它倒序的（查文件出口时是 0） */
  seq: number
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

/** 检索结果的一页：本页条目 + 命中总数（总数只跟筛选条件有关，与当前页码 / 每页条数无关）。 */
export interface LogPage {
  items: LogEntry[]
  total: number
}

/** 检索日志。params 里 undefined 的字段不发，空串照发（owner_id='' 有语义）。 */
export function searchLogs(params: LogSearchParams = {}) {
  return http.get<LogPage>('/logs', { params })
}
