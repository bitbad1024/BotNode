/**
 * 工作流文档的**后端那一半**：载入、暂存、校验、提交版本、发布、运行开关。
 *
 * 编辑器本体因此只剩下「图 + 选中 + 画布交互」；这些动作的共同点是**都要发请求、都要 toast、
 * 都要把后端回来的定义 / 版本 / 校验报告写回 state**，放在一处才不至于散落。
 *
 * 三层数据模型（暂存区 → 版本 → 发布）不在这里解释，见 WorkflowEditor 的文档头。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getDraft,
  getVersion,
  getWorkflow,
  listVersions,
  publishWorkflow,
  saveDraft,
  saveVersion,
  setWorkflowEnabled,
  validateGraph,
  type SaveVersionResultData,
  type ValidationReport,
  type WorkflowData,
  type WorkflowGraph,
  type WorkflowVersionData,
} from '../workflowApi'
import { ApiRequestError } from '../../../lib/http'

export interface WorkflowDocDeps {
  workflowId: string
  /** 当前图（暂存 / 校验 / 提交都发它） */
  graph: WorkflowGraph
  pushToast: (type: 'success' | 'error' | 'info', message: string) => void
  /** 载入完成：由调用方写进图（顺便清撤销栈） */
  onLoaded: (graph: WorkflowGraph) => void
}

export interface WorkflowDoc {
  /** 工作流定义：带 current_ref（当前指向暂存区还是版本）与版本指针 */
  definition: WorkflowData | null
  versions: WorkflowVersionData[]
  /** 最近一次校验报告（null = 没校验过 / 已通过并清空） */
  report: ValidationReport | null
  loading: boolean
  saving: boolean
  /** 拨运行开关的那一下（独立于保存，别互相挡着） */
  switching: boolean
  drafting: boolean
  validating: boolean
  reload: () => Promise<void>
  draft: () => void
  validate: () => void
  save: () => void
  publish: () => void
  toggleEnabled: () => void
}

export function useWorkflowDoc({
  workflowId,
  graph,
  pushToast,
  onLoaded,
}: WorkflowDocDeps): WorkflowDoc {
  const [definition, setDefinition] = useState<WorkflowData | null>(null)
  const [versions, setVersions] = useState<WorkflowVersionData[]>([])
  const [report, setReport] = useState<ValidationReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [switching, setSwitching] = useState(false)
  const [drafting, setDrafting] = useState(false)
  const [validating, setValidating] = useState(false)

  // 载入要读最新的图指针 / 回调，又不想让 reload 每次图变都换身份（键盘快捷键依赖它）
  const latest = useRef({ workflowId, graph, pushToast, onLoaded })
  useEffect(() => {
    latest.current = { workflowId, graph, pushToast, onLoaded }
  })

  const reload = useCallback(async () => {
    const { workflowId: id, pushToast: toast, onLoaded: loaded } = latest.current
    if (!id) return
    setLoading(true)
    try {
      // 定义里的 current_ref 决定打开时看暂存区还是已提交版本
      const { data: def } = await getWorkflow(id)
      const { data: vers } = await listVersions(id)
      setDefinition(def)
      setVersions(vers)

      let loadedGraph: WorkflowGraph | null = null
      if (def.current_ref === 'draft') {
        const { data: draft } = await getDraft(id)
        if (draft.graph) loadedGraph = draft.graph
      }
      // 指针指向版本 / 暂存区为空但已有提交：读对应版本快照（缺省读最新版）
      if (!loadedGraph && def.current_version > 0) {
        const wantVersion = def.current_ref === 'version' ? def.current_version : vers[0]?.version
        if (wantVersion) {
          const { data: snapshot } = await getVersion(id, wantVersion)
          loadedGraph = snapshot.graph
        }
      }
      if (loadedGraph) loaded(loadedGraph)
    } catch (err) {
      toast('error', err instanceof ApiRequestError ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload, workflowId])

  /** 拨开关要读最新定义（按钮点的是「当前开关的反面」） */
  const definitionRef = useRef(definition)
  useEffect(() => {
    definitionRef.current = definition
  }, [definition])

  /** 暂存：不校验，编辑到一半也能存；存完指针留在暂存区。 */
  const draft = useCallback(() => {
    const { workflowId: id, graph: g, pushToast: toast } = latest.current
    if (!id) return
    setDrafting(true)
    void (async () => {
      try {
        const { data } = await saveDraft(id, g)
        setDefinition(data)
        toast('success', '已暂存')
      } catch (err) {
        toast('error', err instanceof ApiRequestError ? err.message : '暂存失败')
      } finally {
        setDrafting(false)
      }
    })()
  }, [])

  const validate = useCallback(() => {
    const { graph: g, pushToast: toast } = latest.current
    setValidating(true)
    void (async () => {
      try {
        const { data } = await validateGraph(g)
        setReport(data)
        if (data.valid) toast('success', '校验通过')
        else {
          toast('error', `校验未通过（${data.stage ?? 'unknown'} 阶段，${data.errors.length} 个错误）`)
        }
      } catch (err) {
        toast('error', err instanceof ApiRequestError ? err.message : '校验失败')
      } finally {
        setValidating(false)
      }
    })()
  }, [])

  /** 提交版本：先校验后写不可变快照，成功后指针切到版本侧。 */
  const save = useCallback(() => {
    const { workflowId: id, graph: g, pushToast: toast } = latest.current
    if (!id) return
    setSaving(true)
    void (async () => {
      try {
        const { data } = await saveVersion(id, g, '画布编辑')
        // 校验不过：后端返回 200 + 校验报告（没有 version/created），不写库
        if ('valid' in data) {
          setReport(data)
          toast('error', `校验未通过（${data.stage}），未提交，请修正后重试`)
          return
        }
        const result: SaveVersionResultData = data
        if (!result.created) {
          toast('info', '内容未变，未产生新版本')
        } else {
          toast('success', `已提交 v${result.version.version}`)
        }
        setDefinition(result.workflow)
        setVersions((v) => [
          result.version,
          ...v.filter((x) => x.version !== result.version.version),
        ])
        setReport(null)
      } catch (err) {
        toast('error', err instanceof ApiRequestError ? err.message : '提交失败')
      } finally {
        setSaving(false)
      }
    })()
  }, [])

  /**
   * 拨**运行开关**：发布 ≠ 运行 —— 拨开才真的按已发布版本跑（默认关）。
   *
   * 还没发布过就点它是没意义的（后端也会 409），所以按钮在那种情况下是禁用的。
   */
  const toggleEnabled = useCallback(() => {
    const { pushToast: toast } = latest.current
    void (async () => {
      const current = definitionRef.current
      if (!current) return
      const next = !current.enabled
      setSwitching(true)
      try {
        const { data } = await setWorkflowEnabled(current.id, next)
        setDefinition(data)
        toast(
          'success',
          next ? `已开启：按已发布版本 v${data.published_version} 跑` : '已停止：不再定时触发',
        )
      } catch (err) {
        toast('error', err instanceof ApiRequestError ? err.message : '开关切换失败')
      } finally {
        setSwitching(false)
      }
    })()
  }, [])

  const publish = useCallback(() => {
    const { workflowId: id, pushToast: toast } = latest.current
    if (!id) return
    void (async () => {
      try {
        const { data } = await publishWorkflow(id)
        setDefinition(data)
        toast('success', '最新版本已发布（重启服务后生效）')
      } catch (err) {
        toast('error', err instanceof ApiRequestError ? err.message : '发布失败')
      }
    })()
  }, [])

  return {
    definition,
    versions,
    report,
    loading,
    saving,
    switching,
    drafting,
    validating,
    reload,
    draft,
    validate,
    save,
    publish,
    toggleEnabled,
  }
}
