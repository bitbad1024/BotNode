/** 工作流画布编辑器：节点拖拽 + 类型化端口连线 + 配置 + 校验 + 保存版本。
 *
 * 端口类型约定：
 * - trigger（触发 / 控制流）：决定"什么时候执行下一个节点"，绿色
 * - message（消息 / 数据流）：传递实际数据内容，蓝色
 *
 * 每种节点有固定的输入/输出端口集合（见 NODE_TYPES），连线时类型必须匹配。
 * 常量输入（config 字段）显示为节点底部的标签条，不可连线，在右侧配置面板编辑。
 *
 * 坐标存 localStorage（后端 WorkflowNode extra=ignore，不存 UI 字段）。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getVersion,
  listVersions,
  publishWorkflow,
  saveVersion,
  validateGraph,
  type NodeType,
  type ValidationIssue,
  type ValidationReport,
  type SaveVersionResultData,
  type WorkflowEdge,
  type WorkflowGraph,
  type WorkflowNode,
  type WorkflowVersionData,
} from './workflowApi'
import { ApiRequestError } from '../../lib/http'
import { useToast } from '../../common/Toast'
import {
  IconArrowLeft,
  IconCheck,
  IconClose,
  IconSave,
  IconTrash,
} from '../../common/icons'
import styles from './WorkflowEditor.module.css'

// --------------------------------------------------------------------------- 端口类型系统

type PortType = 'trigger' | 'message'

interface PortSpec {
  id: string
  type: PortType
  label: string
}

interface NodeTypeDef {
  type: NodeType
  label: string
  color: string
  defaults: Record<string, unknown>
  inputs: PortSpec[]
  outputs: PortSpec[]
  /** 纯常量字段名（不含与端口同名的字段） */
  constants: string[]
}

const PORT_COLORS: Record<PortType, string> = {
  trigger: '#22c55e',
  message: '#3b82f6',
}

const NODE_TYPES: Record<string, NodeTypeDef> = {
  start: {
    type: 'start', label: '开始', color: '#22c55e', defaults: { trigger: 'message' },
    inputs: [],
    outputs: [
      { id: 'trigger', type: 'trigger', label: '触发' },
      { id: 'message', type: 'message', label: '消息' },
    ],
    constants: [],
  },
  end: {
    type: 'end', label: '结束', color: '#ef4444', defaults: {},
    inputs: [{ id: 'trigger', type: 'trigger', label: '触发' }],
    outputs: [],
    constants: [],
  },
  log: {
    type: 'log', label: '写日志', color: '#3b82f6', defaults: { message: '', level: 'INFO' },
    inputs: [
      { id: 'trigger', type: 'trigger', label: '触发' },
      { id: 'message', type: 'message', label: '消息' },
    ],
    outputs: [{ id: 'trigger', type: 'trigger', label: '触发' }],
    constants: ['level'],
  },
  test: {
    type: 'test', label: '测试', color: '#8b5cf6', defaults: { echo: 'hello' },
    inputs: [{ id: 'trigger', type: 'trigger', label: '触发' }],
    outputs: [
      { id: 'trigger', type: 'trigger', label: '触发' },
      { id: 'message', type: 'message', label: '回显' },
    ],
    constants: ['echo'],
  },
  task: {
    type: 'task', label: '任务', color: '#64748b', defaults: {},
    inputs: [
      { id: 'trigger', type: 'trigger', label: '触发' },
      { id: 'message', type: 'message', label: '消息' },
    ],
    outputs: [
      { id: 'trigger', type: 'trigger', label: '触发' },
      { id: 'message', type: 'message', label: '消息' },
    ],
    constants: [],
  },
  http: {
    type: 'http', label: 'HTTP', color: '#0ea5e9', defaults: { url: '', method: 'GET' },
    inputs: [{ id: 'trigger', type: 'trigger', label: '触发' }],
    outputs: [
      { id: 'trigger', type: 'trigger', label: '触发' },
      { id: 'message', type: 'message', label: '响应' },
    ],
    constants: ['url', 'method'],
  },
  condition: {
    type: 'condition', label: '条件', color: '#f97316', defaults: { condition: '' },
    inputs: [
      { id: 'trigger', type: 'trigger', label: '触发' },
      { id: 'message', type: 'message', label: '消息' },
    ],
    outputs: [{ id: 'trigger', type: 'trigger', label: '触发' }],
    constants: ['condition'],
  },
  expression: {
    type: 'expression', label: '表达式', color: '#14b8a6', defaults: { expression: '' },
    inputs: [
      { id: 'trigger', type: 'trigger', label: '触发' },
      { id: 'message', type: 'message', label: '消息' },
    ],
    outputs: [
      { id: 'trigger', type: 'trigger', label: '触发' },
      { id: 'message', type: 'message', label: '结果' },
    ],
    constants: ['expression'],
  },
  gateway: {
    type: 'gateway', label: '网关', color: '#a855f7', defaults: {},
    inputs: [{ id: 'trigger', type: 'trigger', label: '触发' }],
    outputs: [{ id: 'trigger', type: 'trigger', label: '触发' }],
    constants: [],
  },
  approval: {
    type: 'approval', label: '审批', color: '#ec4899', defaults: { assignee: '' },
    inputs: [{ id: 'trigger', type: 'trigger', label: '触发' }],
    outputs: [{ id: 'trigger', type: 'trigger', label: '触发' }],
    constants: ['assignee'],
  },
}

const PALETTE_ORDER: string[] = [
  'start', 'end', 'log', 'test', 'task',
  'http', 'condition', 'expression', 'gateway', 'approval',
]

/** start 节点时间触发形态：只输出触发端口，cron 是常量配置。 */
const START_TIME_DEF: NodeTypeDef = {
  type: 'start',
  label: '开始 · 时间',
  color: '#f59e0b',
  defaults: { trigger: 'time', cron: '*/5 * * * *' },
  inputs: [],
  outputs: [{ id: 'trigger', type: 'trigger', label: '触发' }],
  constants: ['cron'],
}

/** start 节点消息触发形态：输出触发 + 消息端口，无常量配置。 */
const START_MESSAGE_DEF: NodeTypeDef = {
  type: 'start',
  label: '开始 · 消息',
  color: '#22c55e',
  defaults: { trigger: 'message' },
  inputs: [],
  outputs: [
    { id: 'trigger', type: 'trigger', label: '触发' },
    { id: 'message', type: 'message', label: '消息' },
  ],
  constants: [],
}

/**
 * 取节点类型定义；start 的端口 / 常量随 config.trigger 动态变化：
 * time = 只输出触发 + cron 常量；message（含缺省）= 触发 + 消息输出。
 */
function nodeDef(type: string, config?: Record<string, unknown>): NodeTypeDef {
  if (type === 'start') {
    return config?.trigger === 'time' ? START_TIME_DEF : START_MESSAGE_DEF
  }
  return NODE_TYPES[type] ?? {
    type: type as NodeType, label: type, color: '#64748b', defaults: {},
    inputs: [{ id: 'trigger', type: 'trigger', label: '触发' }],
    outputs: [{ id: 'trigger', type: 'trigger', label: '触发' }],
    constants: [],
  }
}

// --------------------------------------------------------------------------- 布局常量

const NODE_W = 168
const HEADER_H = 30
const PORT_ROW_H = 22
const CONST_GAP = 5
const CONST_ROW_H = 18

function nodeHeight(def: NodeTypeDef): number {
  const portRows = Math.max(def.inputs.length, def.outputs.length)
  const constH = def.constants.length > 0 ? CONST_GAP + def.constants.length * CONST_ROW_H : 0
  return HEADER_H + Math.max(portRows, 1) * PORT_ROW_H + constH
}

/** 端口在节点内的 Y 偏移（圆心）。 */
function portCenterY(def: NodeTypeDef, direction: 'in' | 'out', portId: string): number {
  const ports = direction === 'in' ? def.inputs : def.outputs
  const idx = ports.findIndex((p) => p.id === portId)
  if (idx === -1) return HEADER_H + PORT_ROW_H / 2
  return HEADER_H + idx * PORT_ROW_H + PORT_ROW_H / 2
}

/** 端口在画布上的绝对坐标。 */
function portAbsPos(
  nodeId: string,
  portId: string,
  direction: 'in' | 'out',
  nodeType: string,
  positions: Record<string, { x: number; y: number }>,
  config?: Record<string, unknown>,
): { x: number; y: number } | null {
  const pos = positions[nodeId]
  if (!pos) return null
  const def = nodeDef(nodeType, config)
  const y = pos.y + portCenterY(def, direction, portId)
  const x = direction === 'in' ? pos.x : pos.x + NODE_W
  return { x, y }
}

// --------------------------------------------------------------------------- 工具

function uid(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 8)}`
}

function emptyGraph(): WorkflowGraph {
  return { nodes: [], edges: [] }
}

/**
 * 旧版图迁移：独立的 time-trigger 节点已并入 start（config.trigger=time）。
 * 旧图典型结构是 start → time-trigger → ...，直接转换会出现两个 start 违反唯一入口，
 * 所以同时把前置的旧 start 合并掉：旧 start → 迁移节点的边删除，旧 start 的其他出边
 * 改接到迁移节点。用户重新保存后后端快照也完成迁移。
 */
function normalizeGraph(g: WorkflowGraph): WorkflowGraph {
  const migratedIds = new Set<string>()
  const nodes = g.nodes.map((n) => {
    if (n.type !== 'time-trigger') return n
    migratedIds.add(n.id)
    const config: Record<string, unknown> = {
      trigger: 'time',
      cron: typeof n.config.cron === 'string' ? n.config.cron : '*/5 * * * *',
    }
    if (n.config.name !== undefined) config.name = n.config.name
    return { ...n, type: 'start' as const, config }
  })
  if (migratedIds.size === 0) return g

  // 找直接连到迁移节点的旧 start（它们已被迁移节点取代）
  const rewire = new Map<string, string>() // 旧 start id -> 迁移节点 id
  for (const e of g.edges) {
    if (!migratedIds.has(e.target)) continue
    const src = g.nodes.find((n) => n.id === e.source)
    if (src && src.type === 'start') rewire.set(src.id, e.target)
  }

  const edges = g.edges
    // 删掉「旧 start → 迁移节点」这条边（迁移节点自己就是入口了）
    .filter((e) => !(rewire.has(e.source) && migratedIds.has(e.target)))
    // 旧 start 的其他出边改接到迁移节点
    .map((e) => {
      const to = rewire.get(e.source)
      return to ? { ...e, source: to } : e
    })

  return { nodes: nodes.filter((n) => !rewire.has(n.id)), edges }
}

function posKey(workflowId: string): string {
  return `nacho.workflow.pos.${workflowId}`
}

function loadPositions(workflowId: string): Record<string, { x: number; y: number }> {
  try {
    const raw = localStorage.getItem(posKey(workflowId))
    return raw ? (JSON.parse(raw) as Record<string, { x: number; y: number }>) : {}
  } catch {
    return {}
  }
}

function savePositions(workflowId: string, positions: Record<string, { x: number; y: number }>) {
  try {
    localStorage.setItem(posKey(workflowId), JSON.stringify(positions))
  } catch {
    /* ignore */
  }
}

/** 为没有端口信息的旧边推断端口（取第一个类型匹配的端口对）。 */
function inferPorts(srcType: string, tgtType: string): { sourcePort: string; targetPort: string } | null {
  const srcDef = nodeDef(srcType)
  const tgtDef = nodeDef(tgtType)
  for (const out of srcDef.outputs) {
    for (const inp of tgtDef.inputs) {
      if (out.type === inp.type) {
        return { sourcePort: out.id, targetPort: inp.id }
      }
    }
  }
  return null
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 1) + '…' : s
}

// --------------------------------------------------------------------------- 组件

interface WorkflowEditorProps {
  workflowId: string
  onClose: () => void
}

export default function WorkflowEditor({ workflowId, onClose }: WorkflowEditorProps) {
  const { pushToast } = useToast()

  const [graph, setGraph] = useState<WorkflowGraph>(emptyGraph())
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({})
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [report, setReport] = useState<ValidationReport | null>(null)
  const [versions, setVersions] = useState<WorkflowVersionData[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [validating, setValidating] = useState(false)
  const [showPalette, setShowPalette] = useState(true)
  const [showInspector, setShowInspector] = useState(true)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [zoom, setZoom] = useState(1)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [boxSel, setBoxSel] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null)

  const canvasRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ nodeId: string; offsetX: number; offsetY: number } | null>(null)
  const panRef = useRef<{ startX: number; startY: number; panX: number; panY: number } | null>(null)
  const boxRef = useRef<{ startX: number; startY: number } | null>(null)
  /** 正在拖出的连线：起点端口信息 + 鼠标位置 */
  const connectRef = useRef<{
    nodeId: string
    portId: string
    portType: PortType
    direction: 'in' | 'out'
  } | null>(null)
  const [connectCursor, setConnectCursor] = useState<{ x: number; y: number } | null>(null)

  const persistPositions = useCallback(
    (next: Record<string, { x: number; y: number }>) => {
      setPositions(next)
      if (workflowId) savePositions(workflowId, next)
    },
    [workflowId],
  )

  useEffect(() => {
    if (!workflowId) return
    let cancelled = false
    void (async () => {
      setLoading(true)
      try {
        const { data: vers } = await listVersions(workflowId)
        if (!cancelled) {
          setVersions(vers)
          if (vers.length > 0) {
            const { data: latest } = await getVersion(workflowId, vers[0].version)
            if (!cancelled) {
              setGraph(normalizeGraph(latest.graph))
              persistPositions(loadPositions(workflowId))
            }
          } else {
            persistPositions(loadPositions(workflowId))
          }
        }
      } catch (err) {
        pushToast('error', err instanceof ApiRequestError ? err.message : '加载失败')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [workflowId, persistPositions, pushToast])

  // ---- 节点操作 ----
  const addNode = useCallback(
    (type: string) => {
      const def = nodeDef(type)
      const id = uid(type)
      const node: WorkflowNode = {
        id, type: def.type, config: { ...def.defaults }, outputs: [],
      }
      const x = 80 + Math.random() * 200
      const y = 80 + Math.random() * 120
      setGraph((g) => ({ ...g, nodes: [...g.nodes, node] }))
      persistPositions({ ...positions, [id]: { x, y } })
      setSelectedId(id)
    },
    [positions, persistPositions],
  )

  const deleteNode = useCallback(
    (id: string) => {
      setGraph((g) => ({
        nodes: g.nodes.filter((n) => n.id !== id),
        edges: g.edges.filter((e) => e.source !== id && e.target !== id),
      }))
      const next = { ...positions }
      delete next[id]
      persistPositions(next)
      if (selectedId === id) setSelectedId(null)
    },
    [positions, persistPositions, selectedId],
  )

  const deleteSelected = useCallback(() => {
    if (selectedIds.size === 0) return
    setGraph((g) => ({
      nodes: g.nodes.filter((n) => !selectedIds.has(n.id)),
      edges: g.edges.filter((e) => !selectedIds.has(e.source) && !selectedIds.has(e.target)),
    }))
    const next = { ...positions }
    for (const id of selectedIds) delete next[id]
    persistPositions(next)
    setSelectedIds(new Set())
  }, [selectedIds, positions, persistPositions])

  // Delete 键批量删除
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return
      if (selectedIds.size === 0) return
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      e.preventDefault()
      deleteSelected()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [selectedIds, deleteSelected])

  const deleteEdge = useCallback((edge: WorkflowEdge) => {
    setGraph((g) => ({
      ...g,
      edges: g.edges.filter(
        (e) =>
          !(e.source === edge.source &&
            e.target === edge.target &&
            e.sourcePort === edge.sourcePort &&
            e.targetPort === edge.targetPort),
      ),
    }))
  }, [])

  const updateConfig = useCallback((id: string, key: string, value: unknown) => {
    setGraph((g) => ({
      ...g,
      nodes: g.nodes.map((n) =>
        n.id === id ? { ...n, config: { ...n.config, [key]: value } } : n,
      ),
    }))
  }, [])

  /** 切换开始节点的触发方式：time 补默认 cron；message 清掉 cron。 */
  const setStartTrigger = useCallback((id: string, trigger: string) => {
    setGraph((g) => ({
      ...g,
      nodes: g.nodes.map((n) => {
        if (n.id !== id || n.type !== 'start') return n
        const config: Record<string, unknown> = { ...n.config, trigger }
        if (trigger === 'time' && typeof config.cron !== 'string') config.cron = '*/5 * * * *'
        if (trigger === 'message') delete config.cron
        return { ...n, config }
      }),
    }))
  }, [])

  // ---- 拖拽节点 ----
  const onNodeMouseDown = (e: React.MouseEvent, nodeId: string) => {
    if (e.button !== 0) return // 非左键交给画布处理（右键平移）
    if ((e.target as HTMLElement).dataset.role === 'port') return
    e.stopPropagation()
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return
    const pos = positions[nodeId] ?? { x: 0, y: 0 }
    dragRef.current = {
      nodeId,
      offsetX: (e.clientX - rect.left - pan.x) / zoom - pos.x,
      offsetY: (e.clientY - rect.top - pan.y) / zoom - pos.y,
    }
    setSelectedId(nodeId)
  }

  const onCanvasMouseDown = (e: React.MouseEvent) => {
    if (e.button === 2) {
      // 右键：开始平移
      e.preventDefault()
      panRef.current = {
        startX: e.clientX,
        startY: e.clientY,
        panX: pan.x,
        panY: pan.y,
      }
    } else if (e.button === 0) {
      // 左键点空白：准备框选（需要拖动超过阈值才真正开始）
      const rect = canvasRef.current?.getBoundingClientRect()
      if (!rect) return
      const x = (e.clientX - rect.left - pan.x) / zoom
      const y = (e.clientY - rect.top - pan.y) / zoom
      boxRef.current = { startX: x, startY: y }
    }
  }

  const onCanvasMouseMove = (e: React.MouseEvent) => {
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return
    if (panRef.current) {
      const { startX, startY, panX, panY } = panRef.current
      setPan({ x: panX + (e.clientX - startX), y: panY + (e.clientY - startY) })
      return
    }
    const x = (e.clientX - rect.left - pan.x) / zoom
    const y = (e.clientY - rect.top - pan.y) / zoom
    if (boxRef.current) {
      const dx = x - boxRef.current.startX
      const dy = y - boxRef.current.startY
      // 拖动超过阈值才显示框选
      if (!boxSel && Math.abs(dx) < 5 && Math.abs(dy) < 5) return
      if (!boxSel) {
        setBoxSel({ x0: boxRef.current.startX, y0: boxRef.current.startY, x1: x, y1: y })
      } else {
        setBoxSel({ ...boxSel, x1: x, y1: y })
      }
      // 实时计算选中
      const x0 = Math.min(boxRef.current.startX, x)
      const y0 = Math.min(boxRef.current.startY, y)
      const x1 = Math.max(boxRef.current.startX, x)
      const y1 = Math.max(boxRef.current.startY, y)
      const ids = new Set<string>()
      for (const n of graph.nodes) {
        const p = positions[n.id]
        if (!p) continue
        const def = nodeDef(n.type, n.config)
        const h = nodeHeight(def)
        if (p.x + NODE_W >= x0 && p.x <= x1 && p.y + h >= y0 && p.y <= y1) {
          ids.add(n.id)
        }
      }
      setSelectedIds(ids)
      return
    }
    if (dragRef.current) {
      const { nodeId, offsetX, offsetY } = dragRef.current
      persistPositions({
        ...positions,
        [nodeId]: { x: x - offsetX, y: y - offsetY },
      })
    }
    if (connectRef.current) {
      setConnectCursor({ x: (e.clientX - rect.left - pan.x) / zoom, y: (e.clientY - rect.top - pan.y) / zoom })
    }
  }

  // ---- 滚轮缩放 ----
  const onCanvasWheel = (e: React.WheelEvent) => {
    e.preventDefault()
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return
    const mx = e.clientX - rect.left
    const my = e.clientY - rect.top
    const delta = -e.deltaY * 0.0015
    const next = Math.min(3, Math.max(0.25, zoom * (1 + delta)))
    if (next === zoom) return
    // 以鼠标位置为缩放中心：调整 pan 使鼠标下方的画布点不变
    const ratio = next / zoom
    setPan({ x: mx - (mx - pan.x) * ratio, y: my - (my - pan.y) * ratio })
    setZoom(next)
  }

  const onCanvasMouseUp = () => {
    // 如果没拖出框选，是普通点击——交给 onClick 取消选中
    if (boxRef.current && !boxSel) {
      boxRef.current = null
      return
    }
    dragRef.current = null
    connectRef.current = null
    panRef.current = null
    boxRef.current = null
    setConnectCursor(null)
    setBoxSel(null)
  }

  // ---- 端口连线 ----
  const onPortMouseDown = (e: React.MouseEvent, nodeId: string, portId: string, portType: PortType, direction: 'in' | 'out') => {
    if (e.button !== 0) return
    e.stopPropagation()
    connectRef.current = { nodeId, portId, portType, direction }
    const rect = canvasRef.current?.getBoundingClientRect()
    if (rect) setConnectCursor({ x: (e.clientX - rect.left - pan.x) / zoom, y: (e.clientY - rect.top - pan.y) / zoom })
  }

  const onPortMouseUp = (e: React.MouseEvent, nodeId: string, portId: string, portType: PortType, direction: 'in' | 'out') => {
    if (!connectRef.current) return
    e.stopPropagation()
    const drag = connectRef.current
    // 不能连自己
    if (drag.nodeId === nodeId) {
      connectRef.current = null
      setConnectCursor(null)
      return
    }
    // 方向必须一进一出
    if (drag.direction === direction) {
      connectRef.current = null
      setConnectCursor(null)
      return
    }
    // 类型必须匹配
    if (drag.portType !== portType) {
      pushToast('error', `端口类型不匹配：${drag.portType} ≠ ${portType}`)
      connectRef.current = null
      setConnectCursor(null)
      return
    }
    // 确定 source / target
    let source: string, target: string, sourcePort: string, targetPort: string
    if (drag.direction === 'out') {
      source = drag.nodeId
      target = nodeId
      sourcePort = drag.portId
      targetPort = portId
    } else {
      source = nodeId
      target = drag.nodeId
      sourcePort = portId
      targetPort = drag.portId
    }
    // 去重
    setGraph((g) => {
      const exists = g.edges.some(
        (ed) => ed.source === source && ed.target === target && ed.sourcePort === sourcePort && ed.targetPort === targetPort,
      )
      if (exists) return g
      return { ...g, edges: [...g.edges, { source, target, sourcePort, targetPort }] }
    })
    connectRef.current = null
    setConnectCursor(null)
  }

  // ---- 校验 / 保存 / 发布 ----
  const onValidate = useCallback(async () => {
    setValidating(true)
    try {
      const { data } = await validateGraph(graph)
      setReport(data)
      if (data.valid) pushToast('success', '校验通过')
      else pushToast('error', `校验未通过（${data.stage ?? 'unknown'} 阶段，${data.errors.length} 个错误）`)
    } catch (err) {
      pushToast('error', err instanceof ApiRequestError ? err.message : '校验失败')
    } finally {
      setValidating(false)
    }
  }, [graph, pushToast])

  const onSave = useCallback(async () => {
    if (!workflowId) return
    setSaving(true)
    try {
      const { data } = await saveVersion(workflowId, graph, '画布编辑')
      // 校验不过：后端返回 200 + 校验报告（没有 version/created），不写库
      if ('valid' in data) {
        setReport(data)
        pushToast('error', `校验未通过（${data.stage}），未保存，请修正后重试`)
        return
      }
      const result: SaveVersionResultData = data
      if (!result.created) {
        pushToast('info', '内容未变，未产生新版本')
      } else {
        pushToast('success', `已保存 v${result.version.version}`)
      }
      setVersions((v) => [result.version, ...v.filter((x) => x.version !== result.version.version)])
      setReport(null)
    } catch (err) {
      pushToast('error', err instanceof ApiRequestError ? err.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }, [workflowId, graph, pushToast])

  const onPublish = useCallback(async () => {
    if (!workflowId) return
    try {
      await publishWorkflow(workflowId)
      pushToast('success', '最新版本已发布')
    } catch (err) {
      pushToast('error', err instanceof ApiRequestError ? err.message : '发布失败')
    }
  }, [workflowId, pushToast])

  // ---- 渲染辅助 ----
  const selectedNode = graph.nodes.find((n) => n.id === selectedId) ?? null
  const selectedDef = selectedNode ? nodeDef(selectedNode.type, selectedNode.config) : null
  const errorByNode = new Map<string, ValidationIssue[]>()
  if (report) {
    for (const issue of report.errors) {
      const list = errorByNode.get(issue.nodeId) ?? []
      list.push(issue)
      errorByNode.set(issue.nodeId, list)
    }
  }

  /** 计算连线的 SVG 坐标。 */
  function edgeCoords(edge: WorkflowEdge): { x1: number; y1: number; x2: number; y2: number } | null {
    const srcNode = graph.nodes.find((n) => n.id === edge.source)
    const tgtNode = graph.nodes.find((n) => n.id === edge.target)
    if (!srcNode || !tgtNode) return null
    const sp = positions[edge.source]
    const tp = positions[edge.target]
    if (!sp || !tp) return null
    // 推断端口（旧边可能没有端口信息）
    let sourcePortId = edge.sourcePort
    let targetPortId = edge.targetPort
    if (!sourcePortId || !targetPortId) {
      const inferred = inferPorts(srcNode.type, tgtNode.type)
      if (inferred) {
        sourcePortId ??= inferred.sourcePort
        targetPortId ??= inferred.targetPort
      }
    }
    if (!sourcePortId || !targetPortId) return null
    const srcDef = nodeDef(srcNode.type, srcNode.config)
    const tgtDef = nodeDef(tgtNode.type, tgtNode.config)
    return {
      x1: sp.x + NODE_W,
      y1: sp.y + portCenterY(srcDef, 'out', sourcePortId),
      x2: tp.x,
      y2: tp.y + portCenterY(tgtDef, 'in', targetPortId),
    }
  }

  /** 计算临时连线起点。 */
  function dragStartPos(): { x: number; y: number } | null {
    if (!connectRef.current) return null
    const { nodeId, portId, direction } = connectRef.current
    const node = graph.nodes.find((n) => n.id === nodeId)
    if (!node) return null
    return portAbsPos(nodeId, portId, direction, node.type, positions, node.config)
  }

  return (
    <div className={styles.page}>
      {/* 工具栏 */}
      <header className={styles.toolbar}>
        <button className="btn" onClick={onClose}>
          <IconArrowLeft size={15} />
          返回
        </button>
        <div className={styles.toolbarTitle}>
          工作流编辑器
          {versions[0] && <span className={styles.verTag}>当前 v{versions[0].version}</span>}
        </div>
        <div className={styles.toolbarActions}>
          <button className="btn" onClick={() => void onValidate()} disabled={validating}>
            <IconCheck size={14} />
            {validating ? '校验中…' : '校验'}
          </button>
          <button className={`btn ${styles.primary}`} onClick={() => void onSave()} disabled={saving}>
            <IconSave size={14} />
            {saving ? '保存中…' : '保存版本'}
          </button>
          <button className="btn" onClick={() => void onPublish()}>
            发布
          </button>
          <button
            className={`btn ${showPalette ? '' : styles.toggleOff}`}
            onClick={() => setShowPalette((v) => !v)}
            title="切换节点面板"
          >
            节点
          </button>
          <button
            className={`btn ${showInspector ? '' : styles.toggleOff}`}
            onClick={() => setShowInspector((v) => !v)}
            title="切换配置面板"
          >
            配置
          </button>
          <button
            className="btn"
            onClick={() => { setPan({ x: 0, y: 0 }); setZoom(1) }}
            title="重置视图"
          >
            {Math.round(zoom * 100)}%
          </button>
          <button className={`btn ${styles.closeBtn}`} onClick={onClose}>
            <IconClose size={16} />
          </button>
        </div>
      </header>

      <div className={styles.body}>
        {/* 节点面板——悬浮，可折叠 */}
        {showPalette && (
        <aside className={styles.palette}>
          <div className={styles.paletteTitle}>节点</div>
          {PALETTE_ORDER.map((type) => {
            const def = nodeDef(type)
            return (
              <button
                key={type}
                className={styles.paletteItem}
                onClick={() => addNode(type)}
              >
                <span className={styles.paletteDot} style={{ background: def.color }} />
                {def.label}
              </button>
            )
          })}
          <div className={styles.legend}>
            <div className={styles.legendTitle}>端口类型</div>
            <div className={styles.legendRow}>
              <span className={styles.legendDot} style={{ background: PORT_COLORS.trigger }} />
              触发（控制流）
            </div>
            <div className={styles.legendRow}>
              <span className={styles.legendDot} style={{ background: PORT_COLORS.message }} />
              消息（数据流）
            </div>
          </div>
        </aside>
        )}

        {/* 画布 */}
        <div
          ref={canvasRef}
          className={styles.canvas}
          style={{ cursor: panRef.current ? 'grabbing' : 'default' }}
          onContextMenu={(e) => e.preventDefault()}
          onMouseDown={onCanvasMouseDown}
          onMouseMove={onCanvasMouseMove}
          onMouseUp={onCanvasMouseUp}
          onWheel={onCanvasWheel}
          onClick={() => {
            setSelectedId(null)
            setSelectedIds(new Set())
          }}
        >
          {loading ? (
            <div className={styles.loading}>
              <span className="spinner" />
              正在加载…
            </div>
          ) : (
            <div className={styles.canvasContent} style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})` }}>
              <svg className={styles.edges}>
                {graph.edges.map((edge, i) => {
                  const c = edgeCoords(edge)
                  if (!c) return null
                  // 端口颜色
                  const srcNode = graph.nodes.find((n) => n.id === edge.source)
                  const srcDef = srcNode ? nodeDef(srcNode.type, srcNode.config) : null
                  const port = srcDef?.outputs.find((p) => p.id === (edge.sourcePort ?? 'trigger'))
                  const color = port ? PORT_COLORS[port.type] : 'var(--text-3)'
                  return (
                    <g key={i}>
                      <line
                        x1={c.x1} y1={c.y1} x2={c.x2} y2={c.y2}
                        stroke={color}
                        strokeWidth="2"
                      />
                      <circle
                        cx={(c.x1 + c.x2) / 2}
                        cy={(c.y1 + c.y2) / 2}
                        r="7"
                        fill="var(--surface)"
                        stroke="var(--danger)"
                        strokeWidth="1.5"
                        className={styles.edgeDelete}
                        onClick={() => deleteEdge(edge)}
                      />
                    </g>
                  )
                })}
                {(() => {
                  const start = dragStartPos()
                  if (!start || !connectCursor) return null
                  const color = connectRef.current ? PORT_COLORS[connectRef.current.portType] : 'var(--accent)'
                  return (
                    <line
                      x1={start.x} y1={start.y}
                      x2={connectCursor.x} y2={connectCursor.y}
                      stroke={color}
                      strokeWidth="2"
                      strokeDasharray="6 4"
                    />
                  )
                })()}
              </svg>

              {graph.nodes.map((node) => {
                const def = nodeDef(node.type, node.config)
                const pos = positions[node.id] ?? { x: 0, y: 0 }
                const h = nodeHeight(def)
                const hasError = errorByNode.has(node.id)
                const portRows = Math.max(def.inputs.length, def.outputs.length)
                return (
                  <div
                    key={node.id}
                    className={`${styles.node} ${selectedId === node.id ? styles.selected : ''} ${selectedIds.has(node.id) ? styles.boxSelected : ''} ${hasError ? styles.hasError : ''}`}
                    style={{ left: pos.x, top: pos.y, width: NODE_W, height: h }}
                    onMouseDown={(e) => onNodeMouseDown(e, node.id)}
                    onClick={(e) => e.stopPropagation()}
                  >
                    {/* 头部：色条 + 标签 */}
                    <div className={styles.nodeHeader} style={{ '--c': def.color } as React.CSSProperties}>
                      <span className={styles.nodeColorBar} style={{ background: def.color }} />
                      <span className={styles.nodeLabel}>{def.label}</span>
                    </div>

                    {/* 端口区 */}
                    <div className={styles.ports}>
                      {Array.from({ length: portRows }).map((_, rowIdx) => {
                        const inp = def.inputs[rowIdx]
                        const out = def.outputs[rowIdx]
                        return (
                          <div className={styles.portRow} key={rowIdx} style={{ height: PORT_ROW_H }}>
                            {/* 输入端口（左侧） */}
                            <div className={styles.portSide}>
              {inp && (
                <>
                  <span
                    data-role="port"
                    className={styles.portCircle}
                    style={{ left: -7, borderColor: PORT_COLORS[inp.type], background: PORT_COLORS[inp.type] }}
                    onMouseDown={(e) => onPortMouseDown(e, node.id, inp.id, inp.type, 'in')}
                    onMouseUp={(e) => onPortMouseUp(e, node.id, inp.id, inp.type, 'in')}
                  />
                  <span className={styles.portLabel} style={{ color: PORT_COLORS[inp.type] }}>
                    {inp.label}
                  </span>
                </>
              )}
                            </div>
                            {/* 输出端口（右侧） */}
                            <div className={styles.portSideRight}>
              {out && (
                <>
                  <span className={styles.portLabel} style={{ color: PORT_COLORS[out.type] }}>
                    {out.label}
                  </span>
                  <span
                    data-role="port"
                    className={styles.portCircle}
                    style={{ right: -7, borderColor: PORT_COLORS[out.type], background: PORT_COLORS[out.type] }}
                    onMouseDown={(e) => onPortMouseDown(e, node.id, out.id, out.type, 'out')}
                    onMouseUp={(e) => onPortMouseUp(e, node.id, out.id, out.type, 'out')}
                  />
                </>
              )}
                            </div>
                          </div>
                        )
                      })}
                    </div>

                    {/* 常量区 */}
                    {def.constants.length > 0 && (
                      <div className={styles.constArea}>
                        {def.constants.map((key) => {
                          const val = String(node.config[key] ?? '')
                          return (
                            <div className={styles.constPill} key={key}>
                              <span className={styles.constKey}>{key}</span>
                              <span className={styles.constVal}>{truncate(val, 16)}</span>
                            </div>
                          )
                        })}
                      </div>
                    )}

                    {hasError && (
                      <span
                        className={styles.errorBadge}
                        title={errorByNode.get(node.id)?.map((e) => e.message).join('\n')}
                      >
                        {errorByNode.get(node.id)?.length}
                      </span>
                    )}
                  </div>
                )
              })}

              {graph.nodes.length === 0 && (
                <div className={styles.empty}>从左侧点节点名添加到画布</div>
              )}

              {boxSel && (() => {
                const x = Math.min(boxSel.x0, boxSel.x1)
                const y = Math.min(boxSel.y0, boxSel.y1)
                const w = Math.abs(boxSel.x1 - boxSel.x0)
                const h = Math.abs(boxSel.y1 - boxSel.y0)
                return (
                  <svg className={styles.edges} style={{ pointerEvents: 'none' }}>
                    <rect x={x} y={y} width={w} height={h}
                      fill="rgba(99, 102, 241, 0.08)"
                      stroke="var(--accent)"
                      strokeWidth="1"
                      strokeDasharray="4 3"
                    />
                  </svg>
                )
              })()}
            </div>
          )}
        </div>

        {/* 配置面板——悬浮，可折叠 */}
        {showInspector && (
        <aside className={styles.inspector}>
          {selectedNode && selectedDef ? (
            <>
              <div className={styles.inspectorHead}>
                <span className={styles.inspectorTitle}>
                  {selectedDef.label}
                </span>
                <button className={styles.iconBtn} onClick={() => deleteNode(selectedNode.id)}>
                  <IconTrash size={14} />
                </button>
              </div>

              {/* 端口信息 */}
              <div className={styles.portInfo}>
                <div className={styles.portInfoTitle}>端口</div>
                {selectedDef.inputs.length > 0 && (
                  <div className={styles.portInfoSection}>
                    <span className={styles.portInfoLabel}>输入</span>
                    {selectedDef.inputs.map((p) => (
                      <span className={styles.portInfoItem} key={p.id} style={{ color: PORT_COLORS[p.type] }}>
                        ● {p.label}（{p.type}）
                      </span>
                    ))}
                  </div>
                )}
                {selectedDef.outputs.length > 0 && (
                  <div className={styles.portInfoSection}>
                    <span className={styles.portInfoLabel}>输出</span>
                    {selectedDef.outputs.map((p) => (
                      <span className={styles.portInfoItem} key={p.id} style={{ color: PORT_COLORS[p.type] }}>
                        ● {p.label}（{p.type}）
                      </span>
                    ))}
                  </div>
                )}
              </div>

              <div className={styles.field}>
                <label className={styles.label}>节点 ID</label>
                <input className={styles.input} value={selectedNode.id} disabled />
              </div>
              {selectedNode.type === 'start' && (
                <div className={styles.field}>
                  <label className={styles.label}>触发方式</label>
                  <select
                    className={styles.input}
                    value={String(selectedNode.config.trigger ?? 'message')}
                    onChange={(e) => setStartTrigger(selectedNode.id, e.target.value)}
                  >
                    <option value="message">消息触发（无需配置）</option>
                    <option value="time">时间触发（cron 定时）</option>
                  </select>
                </div>
              )}
              {Object.entries(selectedNode.config)
                .filter(([key]) => key !== 'trigger')
                .map(([key, value]) => (
                <div className={styles.field} key={key}>
                  <label className={styles.label}>{key}</label>
                  {key === 'method' ? (
                    <select
                      className={styles.input}
                      value={String(value)}
                      onChange={(e) => updateConfig(selectedNode.id, key, e.target.value)}
                    >
                      <option>GET</option>
                      <option>POST</option>
                      <option>PUT</option>
                      <option>DELETE</option>
                      <option>PATCH</option>
                    </select>
                  ) : key === 'level' ? (
                    <select
                      className={styles.input}
                      value={String(value)}
                      onChange={(e) => updateConfig(selectedNode.id, key, e.target.value)}
                    >
                      <option>DEBUG</option>
                      <option>INFO</option>
                      <option>WARNING</option>
                      <option>ERROR</option>
                      <option>CRITICAL</option>
                    </select>
                  ) : (
                    <input
                      className={styles.input}
                      value={String(value)}
                      onChange={(e) => updateConfig(selectedNode.id, key, e.target.value)}
                    />
                  )}
                </div>
              ))}
              <div className={styles.field}>
                <label className={styles.label}>输出变量（逗号分隔）</label>
                <input
                  className={styles.input}
                  value={selectedNode.outputs.join(',')}
                  onChange={(e) => {
                    const outputs = e.target.value.split(',').map((s) => s.trim()).filter(Boolean)
                    setGraph((g) => ({
                      ...g,
                      nodes: g.nodes.map((n) => (n.id === selectedNode.id ? { ...n, outputs } : n)),
                    }))
                  }}
                />
              </div>
            </>
          ) : (
            <div className={styles.inspectorEmpty}>选中一个节点以编辑配置</div>
          )}

          {report && !report.valid && (
            <div className={styles.errorPanel}>
              <div className={styles.errorTitle}>
                校验失败（{report.stage}）· {report.errors.length} 个问题
              </div>
              {report.errors.map((issue, i) => (
                <div key={i} className={styles.errorItem}>
                  <div className={styles.errorCode}>{issue.code}</div>
                  <div className={styles.errorMsg}>{issue.message}</div>
                  {issue.suggestion && <div className={styles.errorSug}>{issue.suggestion}</div>}
                </div>
              ))}
            </div>
          )}

          {versions.length > 0 && (
            <div className={styles.versions}>
              <div className={styles.versionsTitle}>版本历史</div>
              {versions.map((v) => (
                <div key={v.version} className={styles.versionItem}>
                  <span className={styles.versionNum}>v{v.version}</span>
                  <span className={styles.versionNote}>{v.note || '—'}</span>
                </div>
              ))}
            </div>
          )}
        </aside>
        )}
      </div>
    </div>
  )
}
