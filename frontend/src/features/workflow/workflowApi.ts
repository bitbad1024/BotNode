/** 工作流接口：与后端 /api/workflows/* 一一对应。 */
import { http } from '../../lib/http'

// --------------------------------------------------------------------------- 图类型
/**
 * 节点类型**不在这里枚举**：能摆哪些节点由后端注册表说了算，画布启动时拉
 * ``GET /workflows/node-types``（见下面的 fetchNodeCatalog）—— 加一个节点类型只改后端。
 * 所以 ``type`` 就是普通字符串：认不出的类型（旧图 / 扩展没装）画成灰色未知节点，
 * 保存时会被后端校验的 ``UNKNOWN_NODE_TYPE`` 挡下。
 */
export type PortType = 'trigger' | 'message'

export interface WorkflowNode {
  id: string
  type: string
  config: Record<string, unknown>
  outputs: string[]
  /** 画布坐标：随图持久化（后端快照 / 暂存区都存），但不参与版本 hash */
  x?: number | null
  y?: number | null
}

export interface WorkflowEdge {
  source: string
  target: string
  /** 输出端口 ID（前端连线用；后端会持久化到版本快照里） */
  sourcePort?: string
  /** 输入端口 ID */
  targetPort?: string
  /** 后端返回的下划线写法，normalizeGraph 会归一到驼峰字段 */
  source_port?: string
  target_port?: string
}

export interface WorkflowGraph {
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
}

// --------------------------------------------------------------------------- 节点目录
/** 一个端口（画布上的圆点）：id 就是 edge 的 sourcePort / targetPort。 */
export interface NodePortSpec {
  id: string
  /** 端口类型：连线两端必须同类 */
  type: PortType
  label: string
}

/** config 里的一个字段：画布照它渲染输入框 / 下拉。 */
export interface NodeFieldSpec {
  name: string
  label: string
  required: boolean
  /** default 只在 has_default 时有意义（null 也可能是合法默认值） */
  has_default: boolean
  default: unknown
  /** 有值就是枚举字段：渲染成下拉，顺序即显示顺序 */
  options: string[] | null
}

/** 一种节点类型：画布的面板项 / 标题 / 端口 / 配置表单全从这里来。 */
export interface NodeTypeSpec {
  type: string
  label: string
  role: 'start' | 'end' | 'normal'
  /** 面板顺序（后端已排好：小的在前） */
  order: number
  /** 有没有执行器：声明了但没实现的类型也能存图，跑到它才报错 */
  has_executor: boolean
  min_outgoing: number
  max_outgoing: number | null
  inputs: NodePortSpec[]
  outputs: NodePortSpec[]
  fields: NodeFieldSpec[]
}

export interface NodeCatalog {
  nodes: NodeTypeSpec[]
}

// --------------------------------------------------------------------------- 校验
export interface ValidationIssue {
  nodeId: string
  code: string
  message: string
  suggestion: string
}

export interface ValidationReport {
  valid: boolean
  stage: string | null
  errors: ValidationIssue[]
}

// --------------------------------------------------------------------------- 工作流定义 / 版本
export interface WorkflowData {
  id: string
  owner_id: string
  name: string
  status: 'draft' | 'published'
  /** **运行开关**：发布 ≠ 运行 —— 默认关，拨开才真的按已发布版本跑 */
  enabled: boolean
  current_version: number
  published_version: number
  /** 暂存区最近保存时间（0 = 没暂存过） */
  draft_updated_at: number
  /** 编辑器当前指向：draft = 暂存区，version = 最新提交版本 */
  current_ref: 'draft' | 'version'
  created_at: number
  updated_at: number
}

export interface WorkflowDraftData {
  /** 暂存图；从没暂存过为 null */
  graph: WorkflowGraph | null
  updated_at: number
}

export interface WorkflowVersionData {
  id: string
  workflow_id: string
  owner_id: string
  version: number
  graph: WorkflowGraph
  checksum: string
  note: string
  created_at: number
}

export interface SaveVersionResultData {
  workflow: WorkflowData
  version: WorkflowVersionData
  created: boolean
}

/** 已发布的那一份：定义（含**运行开关**）+ 版本快照（含图）。 */
export interface PublishedWorkflowData {
  workflow: WorkflowData
  version: WorkflowVersionData
}

// --------------------------------------------------------------------------- 接口
/**
 * GET /workflows/node-types：节点类型目录（画布的面板 / 端口 / 配置表单都照它渲染）。
 *
 * 只读后端内存里的注册表，不碰库；要放在 `/workflows/{id}` 之前，后端已经这么声明了。
 */
export function fetchNodeCatalog() {
  return http.get<NodeCatalog>('/workflows/node-types')
}

/** POST /workflows/validate：只校验不入库（画布点「校验」时用）。 */
export function validateGraph(graph: WorkflowGraph) {
  return http.post<ValidationReport, { graph: WorkflowGraph }>('/workflows/validate', { graph })
}

/** POST /workflows：新建工作流（只要名字，图之后逐版存）。 */
export function createWorkflow(name: string) {
  return http.post<WorkflowData, { name: string }>('/workflows', { name })
}

/** GET /workflows：列表（普通用户只看自己的）。 */
export function listWorkflows() {
  return http.get<WorkflowData[]>('/workflows')
}

/** GET /workflows/{id}：单个工作流详情。 */
export function getWorkflow(id: string) {
  return http.get<WorkflowData>(`/workflows/${encodeURIComponent(id)}`)
}

/** PATCH /workflows/{id}：改名。 */
export function renameWorkflow(id: string, name: string) {
  return http.patch<WorkflowData, { name: string }>(`/workflows/${encodeURIComponent(id)}`, { name })
}

/** DELETE /workflows/{id}：删除（级联删版本）。 */
export function deleteWorkflow(id: string) {
  return http.del<void>(`/workflows/${encodeURIComponent(id)}`)
}

/** POST /workflows/{id}/versions：提交一版；校验不通过返回 200 + 校验报告（valid=false），不写库。 */
export function saveVersion(id: string, graph: WorkflowGraph, note = '') {
  return http.post<SaveVersionResultData | ValidationReport, { graph: WorkflowGraph; note: string }>(
    `/workflows/${encodeURIComponent(id)}/versions`,
    { graph, note },
  )
}

/** PUT /workflows/{id}/draft：暂存编辑中的图（不做业务校验，半张图也能存）。 */
export function saveDraft(id: string, graph: WorkflowGraph) {
  return http.put<WorkflowData, { graph: WorkflowGraph }>(
    `/workflows/${encodeURIComponent(id)}/draft`,
    { graph },
  )
}

/** GET /workflows/{id}/draft：读暂存区（没暂存过时 graph 为 null）。 */
export function getDraft(id: string) {
  return http.get<WorkflowDraftData>(`/workflows/${encodeURIComponent(id)}/draft`)
}

/** GET /workflows/{id}/versions：版本历史（倒序，最新在前）。 */
export function listVersions(id: string) {
  return http.get<WorkflowVersionData[]>(`/workflows/${encodeURIComponent(id)}/versions`)
}

/** GET /workflows/{id}/versions/{version}：单个版本快照。 */
export function getVersion(id: string, version: number) {
  return http.get<WorkflowVersionData>(`/workflows/${encodeURIComponent(id)}/versions/${version}`)
}

/** POST /workflows/{id}/publish：发布指定版本；不传 version 发布最新版。 */
export function publishWorkflow(id: string, version?: number) {
  const body: { version?: number } = {}
  if (version !== undefined) body.version = version
  return http.post<WorkflowData, { version?: number }>(
    `/workflows/${encodeURIComponent(id)}/publish`,
    body,
  )
}

/** PUT /workflows/{id}/enabled：拨**运行开关**（发布 ≠ 运行：默认不跑，拨开才跑）。 */
export function setWorkflowEnabled(id: string, enabled: boolean) {
  return http.put<WorkflowData, { enabled: boolean }>(
    `/workflows/${encodeURIComponent(id)}/enabled`,
    { enabled },
  )
}

/** GET /workflows/{id}/published：已发布的那一份（开关状态 + 版本 + 图）。 */
export function getPublishedWorkflow(id: string) {
  return http.get<PublishedWorkflowData>(`/workflows/${encodeURIComponent(id)}/published`)
}
