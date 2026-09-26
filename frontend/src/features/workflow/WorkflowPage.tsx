/** 工作流列表：新建 / 查看 / 发布 / 删除；点编辑弹出全屏画布编辑器。
 *
 * 「新建」是列表卡片右上角那个按钮（点开一个只要名字的小弹窗），不再单独占一块面板 ——
 * 一块列表 + 一个动作，比上下并排两块好看也更好找。
 */
import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  deleteWorkflow,
  listWorkflows,
  publishWorkflow,
  renameWorkflow,
  setWorkflowEnabled,
  type WorkflowData,
} from './workflowApi'
import { ApiRequestError } from '../../lib/http'
import { useToast } from '../../common/Toast'
import {
  IconEdit,
  IconPlus,
  IconRefresh,
  IconSettings,
  IconTrash,
} from '../../common/icons'
import WorkflowCreateDialog from './WorkflowCreateDialog'
import WorkflowEditor from './WorkflowEditor'
import WorkflowSettingsDialog from './WorkflowSettingsDialog'
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
  /** 新建弹窗开着没有（列表右上角那个按钮控制它） */
  const [creating, setCreating] = useState(false)
  const [busy, setBusy] = useState(false)
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [renameId, setRenameId] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  /** 打开设置弹窗的那条工作流（null = 没开） */
  const [settingsFor, setSettingsFor] = useState<WorkflowData | null>(null)

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

  async function commitRename(id: string) {
    const value = renameValue.trim()
    const current = items.find((w) => w.id === id)?.name
    setRenameId(null)
    if (!value || value === current) return
    setBusy(true)
    try {
      const { data } = await renameWorkflow(id, value)
      pushToast('success', `已改名为「${data.name}」`)
      setItems((list) => list.map((w) => (w.id === id ? data : w)))
    } catch (err) {
      pushToast('error', describe(err))
    } finally {
      setBusy(false)
    }
  }

  /** 拨**运行开关**：发布 ≠ 运行 —— 拨开才真的按已发布版本跑（默认关）。 */
  async function toggleEnabled(w: WorkflowData) {
    const next = !w.enabled
    setBusy(true)
    try {
      const { data } = await setWorkflowEnabled(w.id, next)
      pushToast('success', next ? `已开启：${w.name}` : `已停止：${w.name}`)
      setItems((list) => list.map((item) => (item.id === data.id ? data : item)))
    } catch (err) {
      pushToast('error', describe(err))  // 还没发布过就拨开：后端 409
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
          <div className={styles.panelHeadText}>
            <h3 className={styles.panelTitle}>工作流列表</h3>
            <span className={styles.panelNote}>GET /api/workflows</span>
          </div>
          <button className={`btn ${styles.primary}`} onClick={() => setCreating(true)}>
            <IconPlus size={15} />
            新建
          </button>
        </div>
        {loading ? (
          <div className={styles.loading}>
            <span className="spinner" />
            正在加载…
          </div>
        ) : items.length === 0 ? (
          <div className={styles.empty}>还没有工作流，点右上角「新建」建一个吧</div>
        ) : (
          <table className={styles.table}>
            <thead>
              <tr>
                {/* 表头顺序照着下面每行的 td 来：名称 / 状态 / 运行 / 版本 / 时间 / 操作 */}
                <th>名称</th>
                <th>状态</th>
                <th>运行</th>
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
                    <td className={styles.nameCell}>
                      {renameId === w.id ? (
                        <input
                          autoFocus
                          className={styles.renameInput}
                          value={renameValue}
                          maxLength={128}
                          disabled={busy}
                          onChange={(e) => setRenameValue(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') void commitRename(w.id)
                            if (e.key === 'Escape') setRenameId(null)
                          }}
                          onBlur={() => setRenameId(null)}
                        />
                      ) : (
                        w.name
                      )}
                    </td>
                    <td>
                      <span className={`chip ${st.cls}`}>{st.text}</span>
                    </td>
                    <td>
                      <label
                        className={styles.switch}
                        title={
                          w.published_version === 0
                            ? '先发布一版，才能拨运行开关'
                            : w.enabled
                              ? '点一下停止：不再定时触发（发布状态不变）'
                              : '点一下开启：按已发布版本跑'
                        }
                      >
                        <input
                          type="checkbox"
                          checked={w.enabled}
                          disabled={busy || w.published_version === 0}
                          onChange={() => void toggleEnabled(w)}
                        />
                        <span className={styles.switchTrack}>
                          <span className={styles.switchDot} />
                        </span>
                        <span className={styles.switchText}>
                          {w.enabled ? '运行中' : '已停止'}
                        </span>
                      </label>
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
                          disabled={busy}
                          onClick={() => setSettingsFor(w)}
                        >
                          <IconSettings size={14} />
                          设置
                        </button>
                        <button
                          className="btn"
                          disabled={busy || renameId === w.id}
                          onClick={() => {
                            setRenameId(w.id)
                            setRenameValue(w.name)
                          }}
                        >
                          重命名
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

      {creating && (
        <WorkflowCreateDialog
          onClose={() => setCreating(false)}
          onCreated={() => void load()}
        />
      )}

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

      {settingsFor && createPortal(
        <WorkflowSettingsDialog
          workflow={settingsFor}
          onClose={() => setSettingsFor(null)}
          onSaved={(updated) => {
            // 就地替换列表里那一行：设置没动图的指针，不必整页重拉
            setItems((list) => list.map((item) => (item.id === updated.id ? updated : item)))
          }}
        />,
        document.body,
      )}
    </div>
  )
}
