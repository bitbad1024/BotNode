/** 工作流接口：与后端 /api/workflows/* 一一对应。 */
import { http } from '../../lib/http'

// --------------------------------------------------------------------------- 图类型
export type NodeType =
  | 'start'
  | 'end'
  | 'gateway'
  | 'approval'
  | 'expression'
  | 'http'
  | 'condition'
  | 'task'
  | 'log'
  | 'test'
  | 'constant'

export interface WorkflowNode {
  id: string
  type: NodeType | string
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

// --------------------------------------------------------------------------- 接口
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
