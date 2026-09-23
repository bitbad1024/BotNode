/** 工作流列表：新建 / 查看 / 发布 / 删除；点编辑弹出全屏画布编辑器。 */
import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { createPortal } from 'react-dom'
import {
  createWorkflow,
  deleteWorkflow,
  listWorkflows,
  publishWorkflow,
  type WorkflowData,
} from './workflowApi'
import { ApiRequestError } from '../../lib/http'
import { useToast } from '../../common/Toast'
import {
  IconEdit,
  IconPlus,
  IconRefresh,
  IconTrash,
} from '../../common/icons'
import WorkflowEditor from './WorkflowEditor'
import styles from './WorkflowPage.module.css'

function describe(err: unknown): string {
  if (err instanceof ApiRequestError) return err.message
  return '请求失败'
}

function formatTime(unixSeconds: number): string {
  if (!unixSeconds) return '—'
  const d = new Date(unixSeconds * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function statusLabel(w: WorkflowData): { text: string; cls: string } {
  if (w.status === 'published') return { text: '已发布', cls: styles.published }
  return { text: '草稿', cls: styles.draft }
}

export default function WorkflowPage() {
  const { pushToast } = useToast()

  const [items, setItems] = useState<WorkflowData[]>([])
  const [loading, setLoading] = useState(true)
  const [failure, setFailure] = useState('')
  const [name, setName] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [busy, setBusy] = useState(false)
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setFailure('')
    try {
      const { data } = await listWorkflows()
      setItems(data)
    } catch (err) {
      setFailure(describe(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!name.trim()) return
    setSubmitting(true)
    try {
      const { data } = await createWorkflow(name.trim())
      setName('')
      pushToast('success', `工作流「${data.name}」已创建`)
      await load()
    } catch (err) {
      pushToast('error', describe(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function remove(id: string) {
    setBusy(true)
    try {
      await deleteWorkflow(id)
      pushToast('success', '工作流已删除')
      setConfirmId(null)
      await load()
    } catch (err) {
      pushToast('error', describe(err))
    } finally {
      setBusy(false)
    }
  }

  async function publish(id: string) {
    setBusy(true)
    try {
      await publishWorkflow(id)
      pushToast('success', '最新版本已发布')
      await load()
    } catch (err) {
      pushToast('error', describe(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rise">
      <header className={styles.head}>
        <div>
          <h2 className={styles.title}>工作流</h2>
          <p className={styles.desc}>
            用节点 + 连线编排自动化流程：开始节点支持时间触发（cron）与消息触发，
            搭配日志 / 测试等节点，校验通过后保存版本，发布即生效。
          </p>
        </div>
        <button className="btn" onClick={() => void load()} disabled={loading}>
          <IconRefresh size={15} />
          刷新
        </button>
      </header>

      {failure && <div className={styles.failure}>{failure}</div>}

      <section className={`card ${styles.panel}`}>
        <div className={styles.panelHead}>
          <h3 className={styles.panelTitle}>新建工作流</h3>
          <span className={styles.panelNote}>POST /api/workflows</span>
        </div>
        <form className={styles.form} onSubmit={submit}>
          <input
            className={styles.input}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="工作流名称（同账号下唯一）"
            maxLength={128}
          />
          <button className="btn" type="submit" disabled={submitting || !name.trim()}>
            <IconPlus size={15} />
            {submitting ? '创建中…' : '创建'}
          </button>
        </form>
      </section>

      <section className={`card ${styles.panel}`}>
        <div className={styles.panelHead}>
          <h3 className={styles.panelTitle}>工作流列表</h3>
          <span className={styles.panelNote}>GET /api/workflows</span>
        </div>
        {loading ? (
          <div className={styles.loading}>
            <span className="spinner" />
            正在加载…
          </div>
        ) : items.length === 0 ? (
          <div className={styles.empty}>还没有工作流，先创建一个吧</div>
        ) : (
          <table className={styles.table}>
            <thead>
              <tr>
                <th>名称</th>
                <th>状态</th>
                <th>当前版本</th>
                <th>发布版本</th>
                <th>更新时间</th>
                <th style={{ textAlign: 'right' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((w) => {
                const st = statusLabel(w)
                const confirming = confirmId === w.id
                return (
                  <tr key={w.id}>
                    <td className={styles.nameCell}>{w.name}</td>
                    <td>
                      <span className={`chip ${st.cls}`}>{st.text}</span>
                    </td>
                    <td>v{w.current_version}</td>
                    <td>{w.published_version > 0 ? `v${w.published_version}` : '—'}</td>
                    <td className={styles.muted}>{formatTime(w.updated_at)}</td>
                    <td>
                      <div className={styles.rowActions}>
                        <button
                          className="btn"
                          disabled={busy}
                          onClick={() => setEditingId(w.id)}
                        >
                          <IconEdit size={14} />
                          编辑
                        </button>
                        <button
                          className="btn"
                          disabled={busy || w.current_version === 0}
                          onClick={() => void publish(w.id)}
                        >
                          发布最新版
                        </button>
                        {confirming ? (
                          <>
                            <button
                              className={`btn ${styles.solidDanger}`}
                              disabled={busy}
                              onClick={() => void remove(w.id)}
                            >
                              确认删除
                            </button>
                            <button className="btn" onClick={() => setConfirmId(null)}>
                              取消
                            </button>
                          </>
                        ) : (
                          <button
                            className={`btn ${styles.danger}`}
                            disabled={busy}
                            onClick={() => setConfirmId(w.id)}
                          >
                            <IconTrash size={14} />
                            删除
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </section>

      {editingId && createPortal(
        <WorkflowEditor
          workflowId={editingId}
          onClose={() => {
            setEditingId(null)
            void load()
          }}
        />,
        document.body,
      )}
    </div>
  )
}
