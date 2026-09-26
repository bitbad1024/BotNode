/**
 * 令牌管理：签发 / 吊销 OneBot 访问令牌，并查看在线客户端。
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
  fetchClients,
  fetchTokens,
  issueToken,
  kickClient,
  revokeToken,
  setTokenEnabled,
  type IssuedToken,
  type OneBotClient,
  type OneBotToken,
} from './tokensApi'
import { ApiRequestError } from '../../lib/http'
import { copyText } from '../../lib/clipboard'
import { useToast } from '../../common/Toast'
import {
  IconAlert,
  IconCopy,
  IconKey,
  IconPlus,
  IconRefresh,
  IconTrash,
} from '../../common/icons'
import styles from './TokensPage.module.css'

/** 待确认的破坏性操作（吊销令牌 / 断开客户端）。 */
type Pending =
  | { kind: 'revoke'; id: string }
  | { kind: 'kick'; id: string; revoke: boolean }

function formatTime(unixSeconds: number): string {
  if (!unixSeconds) return '—'
  const d = new Date(unixSeconds * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
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

  const [clients, setClients] = useState<OneBotClient[]>([])
  const [tokens, setTokens] = useState<OneBotToken[]>([])
  const [loading, setLoading] = useState(true)
  const [failure, setFailure] = useState('')
  const [pending, setPending] = useState<Pending | null>(null)
  const [busy, setBusy] = useState(false)

  const [account, setAccount] = useState('')
  const [remark, setRemark] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [issued, setIssued] = useState<IssuedToken | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setFailure('')
    try {
      const [c, t] = await Promise.all([fetchClients(), fetchTokens()])
      setClients(c.data)
      setTokens(t.data)
    } catch (err) {
      setFailure(describe(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

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
          <h2 className={styles.title}>令牌管理</h2>
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

      {/* 签发 */}
      <section className={`card ${styles.panel}`}>
        <div className={styles.panelHead}>
          <h3 className={styles.panelTitle}>签发令牌</h3>
        </div>

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
          <button className="btn" type="submit" disabled={submitting}>
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
      </section>

      {/* 在线客户端 */}
      <section className={`card ${styles.panel}`}>
        <div className={styles.panelHead}>
          <h3 className={styles.panelTitle}>在线客户端</h3>
        </div>
        {loading ? (
          <div className={styles.loading}>
            <span className="spinner" />
            正在加载…
          </div>
        ) : clients.length === 0 ? (
          <div className={styles.empty}>当前没有客户端连着</div>
        ) : (
          <table className={styles.table}>
            <thead>
              <tr>
                <th>归属</th>
                <th>机器人号</th>
                <th>对端地址</th>
                <th>连上时间</th>
                <th style={{ textAlign: 'right' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {clients.map((c) => {
                const active =
                  pending?.kind === 'kick' && pending.id === c.client_id ? pending : null
                return (
                  <tr key={c.client_id}>
                    <td>
                      <span className="chip">{c.nickname || c.id || '匿名'}</span>
                    </td>
                    <td className={styles.mono}>{c.self_id ?? '—'}</td>
                    <td className={`${styles.mono} ${styles.muted}`}>{c.remote}</td>
                    <td className={styles.muted}>{formatTime(c.connected_at)}</td>
                    <td>
                      {active ? (
                        <div className={styles.confirm}>
                          <span className={styles.confirmText}>
                            {active.revoke ? '断开并吊销它的令牌？' : '断开这条连接？'}
                          </span>
                          <button
                            className={`btn ${styles.solidDanger}`}
                            disabled={busy}
                            onClick={() =>
                              void run(
                                () => kickClient(c.client_id, active.revoke),
                                active.revoke ? '已断开并吊销令牌' : '已断开连接',
                              )
                            }
                          >
                            确认
                          </button>
                          <button className="btn" onClick={() => setPending(null)}>
                            取消
                          </button>
                        </div>
                      ) : (
                        <div className={styles.rowActions}>
                          <button
                            className="btn"
                            disabled={busy}
                            onClick={() =>
                              setPending({ kind: 'kick', id: c.client_id, revoke: false })
                            }
                          >
                            断开
                          </button>
                          <button
                            className={`btn ${styles.danger}`}
                            disabled={busy}
                            onClick={() =>
                              setPending({ kind: 'kick', id: c.client_id, revoke: true })
                            }
                          >
                            <IconTrash size={14} />
                            断开并吊销
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </section>

      {/* 令牌列表 */}
      <section className={`card ${styles.panel}`}>
        <div className={styles.panelHead}>
          <h3 className={styles.panelTitle}>令牌列表</h3>
        </div>
        {loading ? (
          <div className={styles.loading}>
            <span className="spinner" />
            正在加载…
          </div>
        ) : tokens.length === 0 ? (
          <div className={styles.empty}>还没有任何令牌，客户端连上来会被拒</div>
        ) : (
          <table className={styles.table}>
            <thead>
              <tr>
                <th>归属</th>
                <th>机器人账号</th>
                <th>备注</th>
                <th>签发时间</th>
                <th>状态</th>
                <th style={{ textAlign: 'right' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {tokens.map((t) => {
                const active = pending?.kind === 'revoke' && pending.id === t.id
                return (
                  <tr key={t.id}>
                    <td>
                      <span className="chip">{t.nickname || t.id}</span>
                    </td>
                    <td className={styles.muted}>{t.account || '—'}</td>
                    <td className={t.remark ? undefined : styles.muted}>
                      {t.remark || '—'}
                    </td>
                    <td className={styles.muted}>{formatTime(t.created_at)}</td>
                    <td>
                      <div className={styles.switchRow}>
                        <button
                          type="button"
                          role="switch"
                          aria-checked={t.enabled}
                          aria-label={t.enabled ? '停用令牌' : '启用令牌'}
                          className={`${styles.switch} ${t.enabled ? styles.switchOn : ''}`}
                          disabled={busy}
                          onClick={() =>
                            void run(
                              () => setTokenEnabled(t.id, !t.enabled),
                              t.enabled ? '令牌已停用，客户端已断开' : '令牌已启用',
                            )
                          }
                        >
                          <span className={styles.switchDot} />
                        </button>
                        <span
                          className={t.enabled ? styles.switchOnLabel : styles.muted}
                        >
                          {t.enabled ? '启用' : '停用'}
                        </span>
                      </div>
                    </td>
                    <td>
                      {active ? (
                        <div className={styles.confirm}>
                          <span className={styles.confirmText}>
                            吊销后用它连着的客户端会断开？
                          </span>
                          <button
                            className={`btn ${styles.solidDanger}`}
                            disabled={busy}
                            onClick={() => void run(() => revokeToken(t.id), '令牌已吊销')}
                          >
                            确认
                          </button>
                          <button className="btn" onClick={() => setPending(null)}>
                            取消
                          </button>
                        </div>
                      ) : (
                        <div className={styles.rowActions}>
                          <button
                            className={`btn ${styles.danger}`}
                            disabled={busy}
                            onClick={() => setPending({ kind: 'revoke', id: t.id })}
                          >
                            <IconTrash size={14} />
                            吊销
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}
