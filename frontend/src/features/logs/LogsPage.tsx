/**
 * 运行日志页：GET /api/logs。
 *
 * 数据全部来自后端真实检索，前端不造任何一条日志：查不到就空态，接口挂了就错误态。
 * - 筛选（级别 / 模块 / 关键字 / 时间范围）点「查询」才生效，避免边打边请求；
 * - 管理员额外能选归属（全部 / 仅公共 / 指定 owner_id）与日志来源（落库 / 本机文件）；
 *   普通用户后端强制只返回自己名下的，UI 上直接不露出这两个条件；
 * - 响应没有 total，靠「返回条数 == pageSize」判断是否还有下一页，用「加载更多」翻页；
 * - 可选 10 秒自动刷新：只刷第一页（新日志本来就出现在最前），替换而非拼接。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { searchLogs, LOG_LEVELS, type LogEntry } from './logsApi'
import { ApiRequestError } from '../../lib/http'
import { useAuth } from '../auth/authStore'
import { IconRefresh, IconChevronDown, IconAlert, IconClock } from '../../common/icons'
import styles from './LogsPage.module.css'

/** 每页条数；后端上限 500，这里取 50，靠「加载更多」翻。 */
const PAGE_SIZE = 50
/** 自动刷新间隔（毫秒）。 */
const AUTO_REFRESH_MS = 10_000

/** 级别徽标的配色键，样式按 data-level 走 CSS。 */
type Filters = {
  level: string
  query: string
  loggerName: string
  startTime: string // datetime-local 原值
  endTime: string
  ownerMode: 'all' | 'public' | 'custom'
  ownerId: string
  source: 'database' | 'local' | 'both'
}

const EMPTY_FILTERS: Filters = {
  level: '',
  query: '',
  loggerName: '',
  startTime: '',
  endTime: '',
  ownerMode: 'all',
  ownerId: '',
  source: 'database',
}

/** datetime-local（本地时区）→ Unix 秒字符串；空 / 非法返回 undefined。 */
function toUnix(value: string): string | undefined {
  if (!value) return undefined
  const ms = new Date(value).getTime()
  return Number.isFinite(ms) ? String(Math.floor(ms / 1000)) : undefined
}

function formatTime(unixSeconds: number): string {
  if (!Number.isFinite(unixSeconds)) return '—'
  const d = new Date(unixSeconds * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(
    d.getHours(),
  )}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

const SOURCE_PARAMS: Record<Filters['source'], string | undefined> = {
  database: undefined, // 不写 processors，后端默认就是落库那份
  local: 'local',
  both: 'database,local',
}

export default function LogsPage() {
  const { state } = useAuth()
  const isAdmin = !!state.user?.roles.includes('admin')

  // 表单草稿（输入中）与已生效查询（点「查询」后才同步）
  const [draft, setDraft] = useState<Filters>(EMPTY_FILTERS)
  const [applied, setApplied] = useState<Filters>(EMPTY_FILTERS)

  const [entries, setEntries] = useState<LogEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<{ title: string; detail?: string; traceId?: string } | null>(
    null,
  )
  const [hasMore, setHasMore] = useState(false)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [autoRefresh, setAutoRefresh] = useState(false)

  // 防并发：自动刷新与手动翻页同时在飞时互相让一让
  const inflightRef = useRef(false)

  const patchDraft = (patch: Partial<Filters>) =>
    setDraft((prev) => ({ ...prev, ...patch }))

  /** 把已生效筛选翻译成后端参数（offset 单独传）。 */
  const buildParams = useCallback(
    (filters: Filters, offset: number) => {
      const params: Record<string, string | number> = {
        limit: PAGE_SIZE,
        offset,
      }
      if (filters.level) params.level = filters.level
      const q = filters.query.trim()
      if (q) params.query = q
      const logger = filters.loggerName.trim()
      if (logger) params.logger_name = logger
      const start = toUnix(filters.startTime)
      if (start !== undefined) params.start = start
      const end = toUnix(filters.endTime)
      if (end !== undefined) params.end = end
      if (isAdmin) {
        if (filters.ownerMode === 'public') params.owner_id = '' // 空串=只看公共
        if (filters.ownerMode === 'custom' && filters.ownerId.trim()) {
          params.owner_id = filters.ownerId.trim()
        }
        const processors = SOURCE_PARAMS[filters.source]
        if (processors) params.processors = processors
      }
      return params
    },
    [isAdmin],
  )

  /** 拉日志。append=true 为翻页（拼到尾部），否则替换第一页。 */
  const fetchLogs = useCallback(
    async (filters: Filters, append: boolean) => {
      if (inflightRef.current) return
      inflightRef.current = true
      if (append) setLoadingMore(true)
      else setLoading(true)
      try {
        const offset = append ? entriesRef.current.length : 0
        const res = await searchLogs(buildParams(filters, offset))
        const rows = res.data
        setEntries((prev) => (append ? [...prev, ...rows] : rows))
        setHasMore(rows.length === PAGE_SIZE)
        setError(null)
      } catch (err) {
        if (!append) {
          setEntries([])
          setHasMore(false)
          if (err instanceof ApiRequestError) {
            if (err.status === 503) {
              setError({
                title: '日志没落库，查不了历史',
                detail: '后端需要开启落库出口（[logging.database] enabled = true）后才会有日志可查。',
                traceId: err.traceId,
              })
            } else {
              setError({ title: err.message, traceId: err.traceId })
            }
          } else {
            setError({ title: err instanceof Error ? err.message : '日志加载失败' })
          }
        }
        // 翻页失败不清空已有列表，只提示
        if (append && err instanceof ApiRequestError) {
          setHasMore(false)
        }
      } finally {
        inflightRef.current = false
        setLoading(false)
        setLoadingMore(false)
      }
    },
    [buildParams],
  )

  // entries 的 ref 给异步回调读最新长度，避免闭包过期
  const entriesRef = useRef<LogEntry[]>([])
  entriesRef.current = entries

  // 首次加载 + 已生效查询变化（其实只在提交时 setApplied，首屏一次）
  useEffect(() => {
    void fetchLogs(applied, false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applied])

  // 自动刷新：固定间隔重拉第一页（替换），仅在非手动加载时
  useEffect(() => {
    if (!autoRefresh) return
    const timer = window.setInterval(() => {
      void fetchLogs(applied, false)
    }, AUTO_REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [autoRefresh, applied, fetchLogs])

  function submitSearch(event: React.FormEvent) {
    event.preventDefault()
    setExpandedId(null)
    setApplied({ ...draft })
  }

  function resetFilters() {
    setDraft(EMPTY_FILTERS)
    setApplied(EMPTY_FILTERS)
    setExpandedId(null)
  }

  const activeFilterCount = useMemo(() => {
    let n = 0
    if (draft.level) n++
    if (draft.query.trim()) n++
    if (draft.loggerName.trim()) n++
    if (draft.startTime) n++
    if (draft.endTime) n++
    if (isAdmin) {
      if (draft.ownerMode !== 'all') n++
      if (draft.source !== 'database') n++
    }
    return n
  }, [draft, isAdmin])

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div>
          <h1 className={styles.title}>运行日志</h1>
          <p className={styles.sub}>
            检索框架落库的运行日志，按时间倒序。
            {isAdmin
              ? '你是管理员，可以查看所有人的日志并切换来源。'
              : '普通账号只显示自己名下的日志。'}
          </p>
        </div>
        <div className={styles.headActions}>
          <label className={styles.autoToggle}>
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />
            <IconClock size={15} />
            10 秒自动刷新
          </label>
          <button
            type="button"
            className="btn"
            onClick={() => void fetchLogs(applied, false)}
            disabled={loading}
          >
            <IconRefresh size={15} />
            刷新
          </button>
        </div>
      </header>

      {/* 筛选栏 */}
      <form className={`card ${styles.filterCard}`} onSubmit={submitSearch}>
        <div className={styles.filterGrid}>
          <label className={styles.field}>
            <span className={styles.fieldLabel}>级别</span>
            <select
              className={styles.control}
              value={draft.level}
              onChange={(e) => patchDraft({ level: e.target.value })}
            >
              <option value="">全部级别</option>
              {LOG_LEVELS.map((lv) => (
                <option key={lv} value={lv}>
                  {lv}
                </option>
              ))}
            </select>
          </label>

          <label className={styles.field}>
            <span className={styles.fieldLabel}>关键字（正文模糊）</span>
            <input
              className={styles.control}
              type="text"
              value={draft.query}
              placeholder="如 登录 / 吊销 / trace"
              onChange={(e) => patchDraft({ query: e.target.value })}
            />
          </label>

          <label className={styles.field}>
            <span className={styles.fieldLabel}>模块（精确）</span>
            <input
              className={styles.control}
              type="text"
              value={draft.loggerName}
              placeholder="如 nacho.api"
              onChange={(e) => patchDraft({ loggerName: e.target.value })}
            />
          </label>

          {isAdmin && (
            <>
              <label className={styles.field}>
                <span className={styles.fieldLabel}>归属</span>
                <select
                  className={styles.control}
                  value={draft.ownerMode}
                  onChange={(e) =>
                    patchDraft({ ownerMode: e.target.value as Filters['ownerMode'] })
                  }
                >
                  <option value="all">全部归属</option>
                  <option value="public">仅公共日志</option>
                  <option value="custom">指定 owner_id</option>
                </select>
              </label>

              {draft.ownerMode === 'custom' && (
                <label className={styles.field}>
                  <span className={styles.fieldLabel}>owner_id</span>
                  <input
                    className={styles.control}
                    type="text"
                    value={draft.ownerId}
                    placeholder="如 u-admin"
                    onChange={(e) => patchDraft({ ownerId: e.target.value })}
                  />
                </label>
              )}

              <label className={styles.field}>
                <span className={styles.fieldLabel}>来源</span>
                <select
                  className={styles.control}
                  value={draft.source}
                  onChange={(e) =>
                    patchDraft({ source: e.target.value as Filters['source'] })
                  }
                >
                  <option value="database">落库（默认）</option>
                  <option value="local">本机文件</option>
                  <option value="both">落库 + 本机文件</option>
                </select>
              </label>
            </>
          )}

          <label className={styles.field}>
            <span className={styles.fieldLabel}>开始时间</span>
            <input
              className={styles.control}
              type="datetime-local"
              value={draft.startTime}
              onChange={(e) => patchDraft({ startTime: e.target.value })}
            />
          </label>
          <label className={styles.field}>
            <span className={styles.fieldLabel}>结束时间</span>
            <input
              className={styles.control}
              type="datetime-local"
              value={draft.endTime}
              onChange={(e) => patchDraft({ endTime: e.target.value })}
            />
          </label>
        </div>

        <div className={styles.filterActions}>
          <button type="submit" className="btn btn-primary" disabled={loading}>
            查询
          </button>
          <button
            type="button"
            className="btn"
            onClick={resetFilters}
            disabled={loading || activeFilterCount === 0}
          >
            重置
          </button>
          {activeFilterCount > 0 && (
            <span className={styles.filterCount}>已加 {activeFilterCount} 个条件</span>
          )}
        </div>
      </form>

      {/* 结果区 */}
      <section className={`card ${styles.listCard}`}>
        {loading ? (
          <div className="state-box">
            <IconRefresh size={18} className={styles.spin} />
            正在检索日志…
          </div>
        ) : error ? (
          <div className={styles.errorBox} role="alert">
            <IconAlert size={20} className={styles.errorIcon} />
            <div className={styles.errorBody}>
              <div className={styles.errorTitle}>{error.title}</div>
              {error.detail && <div className={styles.errorDetail}>{error.detail}</div>}
              {error.traceId && (
                <div className={styles.errorTrace}>trace · {error.traceId}</div>
              )}
              <button
                type="button"
                className={`btn ${styles.retryBtn}`}
                onClick={() => void fetchLogs(applied, false)}
              >
                <IconRefresh size={14} />
                重试
              </button>
            </div>
          </div>
        ) : entries.length === 0 ? (
          <div className="state-box">
            <IconAlert size={18} className={styles.stateIcon} />
            没有符合条件的日志
          </div>
        ) : (
          <>
            <ul className={styles.list}>
              {entries.map((entry) => {
                const hasDetail =
                  !!entry.exc_text || Object.keys(entry.extra).length > 0
                const expanded = expandedId === entry.record_id
                return (
                  <li
                    key={entry.record_id}
                    className={`${styles.row} ${expanded ? styles.rowExpanded : ''}`}
                  >
                    <button
                      type="button"
                      className={styles.rowToggle}
                      disabled={!hasDetail}
                      onClick={() =>
                        setExpandedId((id) => (id === entry.record_id ? null : entry.record_id))
                      }
                      aria-expanded={expanded}
                    >
                      <span className={styles.levelBadge} data-level={entry.level}>
                        {entry.level}
                      </span>
                      <span className={styles.rowMain}>
                        <span className={styles.rowMeta}>
                          <span className={styles.time}>{formatTime(entry.timestamp)}</span>
                          <span className={styles.logger}>{entry.logger_name || '—'}</span>
                          {isAdmin && (
                            <span
                              className={`${styles.ownerChip} ${
                                entry.owner_id ? '' : styles.ownerPublic
                              }`}
                            >
                              {entry.owner_id || '公共'}
                            </span>
                          )}
                          {!!entry.exc_text && <span className={styles.excFlag}>异常栈</span>}
                        </span>
                        <span className={styles.message}>{entry.message}</span>
                      </span>
                      {hasDetail && (
                        <IconChevronDown
                          size={16}
                          className={`${styles.chevron} ${expanded ? styles.chevronOpen : ''}`}
                        />
                      )}
                    </button>

                    {expanded && hasDetail && (
                      <div className={styles.detail}>
                        {entry.exc_text && (
                          <pre className={styles.excText}>{entry.exc_text}</pre>
                        )}
                        {Object.keys(entry.extra).length > 0 && (
                          <pre className={styles.extraJson}>
                            {JSON.stringify(entry.extra, null, 2)}
                          </pre>
                        )}
                        <div className={styles.recordId}>record_id · {entry.record_id}</div>
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>

            <div className={styles.pager}>
              {hasMore ? (
                <button
                  type="button"
                  className="btn"
                  onClick={() => void fetchLogs(applied, true)}
                  disabled={loadingMore}
                >
                  <IconRefresh size={14} className={loadingMore ? styles.spin : undefined} />
                  {loadingMore ? '正在加载…' : '加载更多'}
                </button>
              ) : (
                <span className={styles.pagerEnd}>— 已经到底了 —</span>
              )}
            </div>
          </>
        )}
      </section>
    </div>
  )
}
