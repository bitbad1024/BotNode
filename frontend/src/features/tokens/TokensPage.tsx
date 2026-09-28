/**
 * 机器人：签发 / 吊销 OneBot 访问令牌，一卡一个机器人，点「详细」看连接信息。
 *
 * 令牌决定「连进来的机器人属于谁」——归属就是当前登录用户，**不用填**；页面上要紧的两件事：
 *
 * - **明文只在签发那一次出现**：库里只存摘要，关掉提示就拿不回来了，所以签出来
 *   立刻整块显示 + 一键复制，并写明后果；
 * - **删一个客户端要走两条腿**：OneBot 实现断线都会自动重连，只断开不吊销的话
 *   过几秒它又回到列表里 —— 所以「断开」与「断开并吊销」是两个按钮，说清区别；
 * - **停用 ≠ 吊销**：停用只是不许再连（记录还在，开关能拨回来，所以不弹二次确认），
 *   吊销是删记录、不可逆，所以那颗按钮要走确认。
 */
import { useCallback, useEffect, useState, type FormEvent } from 'react'
import {
  fetchTokens,
  issueToken,
  revokeToken,
  setTokenEnabled,
  type IssuedToken,
  type OneBotToken,
} from './tokensApi'
import { ApiRequestError } from '../../lib/http'
import { copyText } from '../../lib/clipboard'
import { useToast } from '../../common/Toast'
import { ConfirmDialog } from '../../common/ConfirmDialog'
import { CardGridSkeleton } from '../../common/Skeleton'
import { Modal } from '../../common/Modal'
import {
  IconAlert,
  IconCopy,
  IconKey,
  IconPlus,
  IconRefresh,
  IconTrash,
} from '../../common/icons'
import styles from './TokensPage.module.css'

/** 待确认的破坏性操作（吊销令牌）。 */
type Pending = { kind: 'revoke'; id: string }

function formatTime(unixSeconds: number): string {
  if (!unixSeconds) return '—'
  const d = new Date(unixSeconds * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 在线时长的展示格式：x 小时 y 分 z 秒（不足一小时就只到分）。前端实时算，不走接口。 */
function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h} 小时 ${pad(m)} 分 ${pad(sec)} 秒` : `${m} 分 ${pad(sec)} 秒`
}

function describe(err: unknown): string {
  if (err instanceof ApiRequestError) {
    // 503 = 主程序没把 OneBot 服务传给 create_app；说清楚比甩一个状态码有用
    if (err.status === 503) return 'OneBot 未接入（主程序没有传入服务）'
    return err.message
  }
  return '请求失败'
}

export default function TokensPage() {
  const { pushToast } = useToast()

  const [tokens, setTokens] = useState<OneBotToken[]>([])
  const [loading, setLoading] = useState(true)
  const [failure, setFailure] = useState('')
  const [pending, setPending] = useState<Pending | null>(null)
  const [busy, setBusy] = useState(false)

  const [account, setAccount] = useState('')
  const [remark, setRemark] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [issued, setIssued] = useState<IssuedToken | null>(null)
  // 「添加机器人」弹窗是否打开（签发表单从顶部面板移到了弹窗里）
  const [issueOpen, setIssueOpen] = useState(false)
  // 点开的令牌卡片（看它的连接详情）；展开时每秒拨一次时钟算在线时长
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [now, setNow] = useState(() => Date.now())

  const load = useCallback(async () => {
    setLoading(true)
    setFailure('')
    try {
      const { data } = await fetchTokens()
      setTokens(data)
    } catch (err) {
      setFailure(describe(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // 有展开的卡片时每秒刷新一次时钟：在线时长是前端实时算的
  useEffect(() => {
    if (!expandedId) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [expandedId])

  /** 破坏性操作统一走这里：跑完刷新列表，失败弹提示。 */
  async function run(action: () => Promise<unknown>, okMessage: string) {
    setBusy(true)
    try {
      await action()
      pushToast('success', okMessage)
      await load()
    } catch (err) {
      pushToast('error', describe(err))
    } finally {
      setBusy(false)
      setPending(null)
    }
  }

  /** 确认弹窗里点「确认」：吊销令牌。 */
  function confirmPending() {
    if (!pending) return
    void run(() => revokeToken(pending.id), '令牌已吊销')
  }

  /** 弹窗正文里点名的对象：吊销的那张令牌（找不到就退回「这个令牌」）。 */
  const revokeTarget = tokens.find((t) => t.id === pending?.id) ?? null

  async function submit(event: FormEvent) {
    event.preventDefault()
    setSubmitting(true)
    try {
      const { data } = await issueToken(account.trim(), remark.trim())
      setIssued(data)
      setAccount('')
      setRemark('')
      pushToast('success', '令牌已签发，请立即保存明文')
      await load()
    } catch (err) {
      pushToast('error', describe(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function copy(token: string) {
    try {
      await copyText(token)
      pushToast('success', '明文令牌已复制到剪贴板')
    } catch {
      pushToast('error', '复制失败，请手动选中复制')
    }
  }

  return (
    <div className="rise">
      <header className={styles.head}>
        <div>
          <h2 className={styles.title}>机器人</h2>
          <p className={styles.desc}>
            令牌决定「连进来的机器人属于谁」——归属就是当前登录用户，不用填。明文只在签发时
            显示一次，之后查不回来。停用只是不许再连（可随时启用回来）；吊销则是删掉记录、
            不可逆。两者都会把用它连着的客户端一并断开。
          </p>
        </div>
        <div className={styles.actions}>
          <button className="btn" onClick={() => void load()} disabled={loading}>
            <IconRefresh size={15} />
            刷新
          </button>
        </div>
      </header>

      {failure && (
        <div className={styles.failure}>
          <IconAlert size={16} />
          {failure}
        </div>
      )}

      {/* 令牌列表 */}
      <section className={`card ${styles.panel}`}>
        <div className={styles.panelHead}>
          <h3 className={styles.panelTitle}>令牌列表</h3>
        </div>
        {loading ? (
          <CardGridSkeleton />
        ) : (
          <div className={styles.cardGrid}>
            {/* 添加机器人：和机器人卡片同尺寸的入口，点开弹签发表单 */}
            <button
              type="button"
              className={styles.addCard}
              onClick={() => setIssueOpen(true)}
            >
              <span className={styles.addCardIcon}>
                <IconPlus size={22} />
              </span>
              <span className={styles.addCardTitle}>添加机器人</span>
              <span className={styles.addCardHint}>签发访问令牌 · 明文只显示一次</span>
            </button>
            {tokens.map((t) => {
              return (
                <div
                  key={t.id}
                  className={`${styles.card} ${expandedId === t.id ? styles.cardOpen : ''}`}
                  onClick={() => setExpandedId(expandedId === t.id ? null : t.id)}
                >
                  <div className={styles.cardTop}>
                    <span className="chip">{t.nickname || t.id}</span>
                    <div className={styles.statusRow}>
                      <span
                        className={`${styles.dot} ${t.online ? styles.dotOn : styles.dotOff}`}
                      />
                      <span className={t.online ? styles.onlineLabel : styles.muted}>
                        {t.online ? '在线' : '离线'}
                      </span>
                      {t.online && t.clients.length > 1 && (
                        <span className={styles.muted}>{t.clients.length} 条</span>
                      )}
                    </div>
                  </div>
                  <dl className={styles.cardBody}>
                    <div className={styles.cardField}>
                      <dt>机器人账号</dt>
                      <dd className={t.account ? undefined : styles.muted}>
                        {t.account || '—'}
                      </dd>
                    </div>
                    <div className={styles.cardField}>
                      <dt>备注</dt>
                      <dd className={t.remark ? undefined : styles.muted}>
                        {t.remark || '—'}
                      </dd>
                    </div>
                    <div className={styles.cardField}>
                      <dt>签发时间</dt>
                      <dd className={styles.muted}>{formatTime(t.created_at)}</dd>
                    </div>
                  </dl>
                  <div className={styles.cardFoot}>
                    <div className={styles.switchRow}>
                      <button
                        type="button"
                        role="switch"
                        aria-checked={t.enabled}
                        aria-label={t.enabled ? '停用令牌' : '启用令牌'}
                        className={`switch ${t.enabled ? 'switch-on' : ''}`}
                        disabled={busy}
                        onClick={(e) => {
                          e.stopPropagation() // 拨开关不展开卡片
                          void run(
                            () => setTokenEnabled(t.id, !t.enabled),
                            t.enabled ? '令牌已停用，客户端已断开' : '令牌已启用',
                          )
                        }}
                      >
                        <span className="switch-knob" />
                      </button>
                      <span className={t.enabled ? styles.switchOnLabel : styles.muted}>
                        {t.enabled ? '启用' : '停用'}
                      </span>
                    </div>
                    <button
                      className="btn btn-danger-ghost"
                      disabled={busy}
                      onClick={(e) => {
                        e.stopPropagation()
                        setPending({ kind: 'revoke', id: t.id })
                      }}
                    >
                      <IconTrash size={14} />
                      吊销
                    </button>
                  </div>
                  {expandedId === t.id && (
                    <div className={styles.cardDetail}>
                      {t.online && t.clients[0] ? (
                        <dl>
                          <div>
                            <dt>QQ 号</dt>
                            <dd>{t.clients[0].self_id ?? '—'}</dd>
                          </div>
                          <div>
                            <dt>对端地址</dt>
                            <dd>{t.clients[0].remote}</dd>
                          </div>
                          <div>
                            <dt>连接时间</dt>
                            <dd>{formatTime(t.clients[0].connected_at)}</dd>
                          </div>
                          <div>
                            <dt>在线时长</dt>
                            <dd>
                              {formatDuration(now / 1000 - t.clients[0].connected_at)}
                            </dd>
                          </div>
                        </dl>
                      ) : (
                        <p className={styles.cardEmpty}>当前没有客户端连着这张令牌</p>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
            {tokens.length === 0 && (
              <div className={styles.emptyGuide}>
                <IconKey size={16} />
                <div>
                  <b>还没有机器人</b>
                  <p>
                    点「添加机器人」签发访问令牌，明文只显示这一次。把明文配到 OneBot
                    实现的反向 WS 地址上，它连进来就归到这个机器人下。
                  </p>
                </div>
              </div>
            )}
          </div>
        )}
      </section>

      {/* 添加机器人：签发表单弹窗（明文只在这次出现，先复制再关） */}
      {issueOpen && (
        <Modal title="添加机器人" onClose={() => setIssueOpen(false)}>
          <form className={styles.form} onSubmit={submit}>
            <div className={styles.field}>
              <label className={styles.label} htmlFor="token-account">
                机器人账号（可选）
              </label>
              <input
                id="token-account"
                className={styles.input}
                value={account}
                onChange={(e) => setAccount(e.target.value)}
                placeholder="接入 WS 的那个 OneBot 账号（只用来展示）"
                maxLength={64}
                autoFocus
              />
            </div>
            <div className={styles.field}>
              <label className={styles.label} htmlFor="token-remark">
                备注（可选）
              </label>
              <input
                id="token-remark"
                className={styles.input}
                value={remark}
                onChange={(e) => setRemark(e.target.value)}
                placeholder="如 主号"
                maxLength={255}
              />
            </div>
            <button className="btn btn-primary" type="submit" disabled={submitting}>
              <IconPlus size={15} />
              {submitting ? '签发中…' : '签发'}
            </button>
          </form>
          {issued && (
            <div className={styles.issued}>
              <div className={styles.issuedHead}>
                <IconKey size={16} />
                令牌已签发给「{issued.record.nickname || issued.record.id}」——明文只显示这一次
              </div>
              <div className={styles.issuedRow}>
                <code className={styles.issuedText}>{issued.token}</code>
                <button className="btn" onClick={() => void copy(issued.token)}>
                  <IconCopy size={15} />
                  复制
                </button>
              </div>
              <div className={styles.issuedHint}>
                关掉这条提示就拿不回来了（库里只存摘要）。把明文配到 OneBot 实现的反向 WS
                地址上，它连进来就归到这个账号下。
              </div>
            </div>
          )}
        </Modal>
      )}

      {/* 破坏性操作统一走确认弹窗：吊销 */}
      {pending && (
        <ConfirmDialog
          title="吊销这个令牌？"
          body={
            <>
              吊销 <b>{revokeTarget?.nickname || revokeTarget?.account || '这个令牌'}</b>
              ：用它连着的客户端会立刻断开，记录被删除、不可恢复（停用可以再启用，吊销不行）。
            </>
          }
          confirmText="吊销"
          danger
          busy={busy}
          onCancel={() => setPending(null)}
          onConfirm={() => void confirmPending()}
        />
      )}
    </div>
  )
}
