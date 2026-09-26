/** 工作流画布编辑器：节点拖拽 + 类型化端口连线 + 配置 + 校验 + 暂存 / 提交版本 / 发布。
 *
 * 工作流分三层：
 * - 暂存区（draft）：编辑中的图，随时「暂存」，不做校验，坐标等 UI 字段也存在里面；
 * - 版本（version）：校验通过后「保存版本」生成不可变快照，坐标不参与 hash；
 * - 发布（published）：发布只挪指针，服务启动时统一把已发布工作流载入调度器。
 *
 * 端口类型约定：
 * - trigger（触发 / 控制流）：决定"什么时候执行下一个节点"，绿色
 * - message（消息 / 数据流）：传递实际数据内容，蓝色
 *
 * 每种节点的输入/输出端口由后端注册表定义（GET /workflows/node-types），连线时类型必须匹配。
 * 常量输入（config 字段）显示为节点底部的标签条，不可连线，在右侧配置面板编辑。
 *
 * 节点坐标直接存在节点 x/y 上随图提交；旧版坐标在 localStorage 里，加载时自动迁移。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  fetchNodeCatalog,
  getDraft,
  getVersion,
  getWorkflow,
  listVersions,
  publishWorkflow,
  saveDraft,
  saveVersion,
  setWorkflowEnabled,
  validateGraph,
  type NodeFieldSpec,
  type NodePortSpec,
  type NodeTypeSpec,
  type PortType,
  type ValidationIssue,
  type ValidationReport,
  type SaveVersionResultData,
  type WorkflowData,
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

// --------------------------------------------------------------------------- 节点类型（来自后端目录）
/**
 * 节点类型**不在前端定义**：编辑器启动时拉一次 ``GET /workflows/node-types``（见 workflowApi
 * 的 ``fetchNodeCatalog``），面板项 / 标题 / 端口 / 配置字段都按后端给的渲染。这里只留两样：
 *
 * - ``NODE_COLORS``：颜色是皮肤，后端不管，认不出的类型用灰的；
 * - 两个**固有例外**（它们的形状本来就随 config 变，不是「前端另有定义」）：
 *   ``start`` 的端口与常量条随 ``config.trigger`` 变；``constant`` 的常量就是它的 config 本身。
 */
type PortSpec = NodePortSpec

const PORT_COLORS: Record<PortType, string> = {
  trigger: '#22c55e',
  message: '#3b82f6',
}

//: 节点配色（皮肤）：后端只给类型名与显示名，颜色由这里定
const NODE_COLORS: Record<string, string> = {
  start: '#22c55e',
  end: '#ef4444',
  log: '#3b82f6',
  test: '#8b5cf6',
  http: '#0ea5e9',
  constant: '#eab308',
}

const DEFAULT_COLOR = '#64748b'

/** start 时间形态换色（面板上「开始」只有一个入口，节点按触发方式区分） */
const START_TIME_COLOR = '#f59e0b'

interface NodeTypeDef {
  type: string
  label: string
  color: string
  defaults: Record<string, unknown>
  inputs: PortSpec[]
  outputs: PortSpec[]
  /** 卡片底部的常量条：后端声明的字段里，名字**不是**端口的那些 */
  constants: string[]
  /** 配置面板照它渲染（同样不含端口同名字段——那些交给端口交互） */
  fields: NodeFieldSpec[]
}

//: 拉回来的目录：类型 -> 规格（渲染时按类型取；面板顺序也来自它）
let CATALOG: Record<string, NodeTypeSpec> = {}

/** 装目录（编辑器加载时调一次），返回已按后端 order 排好的面板项列表。 */
function installCatalog(nodes: NodeTypeSpec[]): NodeTypeSpec[] {
  CATALOG = Object.fromEntries(nodes.map((item) => [item.type, item]))
  return [...nodes].sort((a, b) => a.order - b.order)
}

/** 后端没登记这个类型时的兜底端口：能画、能接线，保存时被 ``UNKNOWN_NODE_TYPE`` 拦下。 */
const UNKNOWN_PORTS: PortSpec[] = [{ id: 'trigger', type: 'trigger', label: '触发' }]

/**
 * 取节点类型定义（渲染用）：端口 / 字段 / 中文名 / 顺序全部来自后端目录，前端只补颜色，
 * 并按 config 处理上面说的两个固有例外。
 */
function nodeDef(type: string, config?: Record<string, unknown>): NodeTypeDef {
  const spec = CATALOG[type]
  if (spec === undefined) {
    // 认不出的类型：不猜它的端口，只给一对触发口让它还能画出来（这是兜底，不是定义）
    return {
      type, label: type, color: DEFAULT_COLOR, defaults: {},
      inputs: UNKNOWN_PORTS, outputs: UNKNOWN_PORTS, constants: [], fields: [],
    }
  }

  const portIds = new Set([...spec.inputs, ...spec.outputs].map((port) => port.id))
  const defaults: Record<string, unknown> = {}
  for (const field of spec.fields) {
    if (field.has_default) defaults[field.name] = field.default
  }

  const base: NodeTypeDef = {
    type: spec.type,
    label: spec.label,
    color: NODE_COLORS[spec.type] ?? DEFAULT_COLOR,
    defaults,
    inputs: spec.inputs,
    outputs: spec.outputs,
    // 卡片底部的常量条：与端口同名的字段（log 的 message）是**消息入口**，卡片上不重复显示
    constants: spec.fields
      .filter((field) => !portIds.has(field.name))
      .map((field) => field.name),
    // 配置面板**照单全收**：message 既是端口又是必填配置，必须能填（滤掉就永远填不上了）
    fields: [...spec.fields],
  }

  if (spec.type === 'start') {
    // 例外一：时间形态只出触发端口，卡片上只显示 cron（message 形态两者都不显示）
    if (config?.trigger !== 'time') return { ...base, constants: [] }
    return {
      ...base,
      label: `${base.label} · 时间`,
      color: START_TIME_COLOR,
      outputs: base.outputs.filter((port) => port.id === 'trigger'),
      constants: ['cron'],
    }
  }
  if (spec.type === 'constant') {
    // 例外二：常量节点的「常量」就是它的 config，几个键就在卡片上排几条（高度跟着长）
    return { ...base, constants: Object.keys(config ?? {}) }
  }
  return base
}

/**
 * 哪些字段**有专门的编辑器**，通用渲染要跳过（不然会出现两个控件）。
 *
 * 目前只有 start 的 ``trigger``：改它得顺手增删 cron（见 ``setStartTrigger``），不是单纯
 * 改一个值。
 */
function hasDedicatedEditor(nodeType: string, fieldName: string): boolean {
  return nodeType === 'start' && fieldName === 'trigger'
}

/** start 的触发方式选项同样来自后端目录；label 用一句人话解释，认不出的值原样显示。 */
const TRIGGER_LABELS: Record<string, string> = {
  message: '消息触发（无需配置）',
  time: '时间触发（cron 定时）',
}

function triggerOptionsOf(): string[] {
  const field = CATALOG['start']?.fields.find((item) => item.name === 'trigger')
  return field?.options ?? ['message']
}

// --------------------------------------------------------------------------- 布局常量

const NODE_W = 168
const HEADER_H = 34
const PORT_ROW_H = 24
// 常量区只服务于「框选命中估算」（卡片高度本身已由内容撑开）：按每个胶囊独占一行的
// 宽裕口径算，框选宁多勿漏
const CONST_GAP = 13
const CONST_ROW_H = 22

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

/**
 * 连线的贝塞尔路径：水平控制点让线平滑绕行（目标在左边也画得出自然的 S 形）。
 * 控制点偏移随水平距离伸缩，太近也不小于 36px，保证曲线不塌成直角。
 */
function edgeCurve(x1: number, y1: number, x2: number, y2: number): string {
  const bend = Math.min(120, Math.max(36, Math.abs(x2 - x1) / 2))
  return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`
}

// --------------------------------------------------------------------------- 工具

function uid(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 8)}`
}

function emptyGraph(): WorkflowGraph {
  return { nodes: [], edges: [] }
}

/**
 * 后端形态归一：边的端口字段后端是 source_port/target_port，统一成前端用的驼峰写法。
 *
 * （旧版独立 time-trigger 节点并入 start 的迁移已随旧快照一起删掉了 —— 现在只可能是
 * start + config.trigger=time，见 nodes/start.py。）
 */
function normalizeGraph(g: WorkflowGraph): WorkflowGraph {
  const normEdge = (e: WorkflowEdge): WorkflowEdge => ({
    source: e.source,
    target: e.target,
    sourcePort: e.sourcePort ?? e.source_port,
    targetPort: e.targetPort ?? e.target_port,
  })

  return { nodes: g.nodes, edges: g.edges.map(normEdge) }
}

/**
 * 坐标迁移：旧版坐标只存在 localStorage，节点自身没 x/y。加载旧图时把本地坐标
 * 补到节点上（下次暂存 / 提交就随图持久化到后端）；节点已带坐标的以图里的为准。
 */
function withLegacyPositions(
  g: WorkflowGraph,
  legacy: Record<string, { x: number; y: number }>,
): WorkflowGraph {
  return {
    ...g,
    nodes: g.nodes.map((n) => {
      if (typeof n.x === 'number' && typeof n.y === 'number') return n
      const p = legacy[n.id]
      return p ? { ...n, x: p.x, y: p.y } : n
    }),
  }
}

function posKey(workflowId: string): string {
  return `nacho.workflow.pos.${workflowId}`
}

/** 读旧版本地坐标（仅用于迁移；新坐标随图存取，不再写 localStorage）。 */
function loadPositions(workflowId: string): Record<string, { x: number; y: number }> {
  try {
    const raw = localStorage.getItem(posKey(workflowId))
    return raw ? (JSON.parse(raw) as Record<string, { x: number; y: number }>) : {}
  } catch {
    return {}
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
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [report, setReport] = useState<ValidationReport | null>(null)
  const [versions, setVersions] = useState<WorkflowVersionData[]>([])
  /** 工作流定义：带 current_ref（当前指向暂存区还是版本）与版本指针 */
  const [definition, setDefinition] = useState<WorkflowData | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  /** 拨运行开关的那一下（独立于保存，别互相挡着） */
  const [switching, setSwitching] = useState(false)
  const [drafting, setDrafting] = useState(false)
  const [validating, setValidating] = useState(false)
  const [showPalette, setShowPalette] = useState(true)
  const [showInspector, setShowInspector] = useState(true)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [zoom, setZoom] = useState(1)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [boxSel, setBoxSel] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null)
  /**
   * 节点类型目录（后端给的）：拉回来之前**不渲染画布** —— 认不出类型就画不出端口。
   * 失败也不退回一份前端定义（那正是以前漂移的来源），只给一个重试。
   */
  const [palette, setPalette] = useState<NodeTypeSpec[] | null>(null)
  const [catalogFailed, setCatalogFailed] = useState(false)

  const canvasRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ nodeId: string; offsetX: number; offsetY: number } | null>(null)
  const panRef = useRef<{ startX: number; startY: number; panX: number; panY: number } | null>(null)
  const boxRef = useRef<{ startX: number; startY: number } | null>(null)
  /** 本次空白拖拽是否已越过阈值进入框选（松手时区分「点了一下」与「框选完」） */
  const boxMovedRef = useRef(false)
  /** 框选结束的松手会被浏览器补发一发 click，用它立牌子吞掉（见 onCanvasMouseUp） */
  const suppressClickRef = useRef(false)
  /** 正在拖出的连线：起点端口信息 + 鼠标位置 */
  const connectRef = useRef<{
    nodeId: string
    portId: string
    portType: PortType
    direction: 'in' | 'out'
  } | null>(null)
  const [connectCursor, setConnectCursor] = useState<{ x: number; y: number } | null>(null)

  /** 旧版本地坐标（仅迁移用一次）：节点没带 x/y 时兜底布局 */
  const legacyPositionsRef = useRef<Record<string, { x: number; y: number }>>(
    workflowId ? loadPositions(workflowId) : {},
  )

  /**
   * 坐标直接从节点 x/y 派生（渲染 / 框选 / 连线都读它）；
   * 没坐标的旧节点用 localStorage 里的遗留坐标兜底。
   */
  const positions = useMemo(() => {
    const map: Record<string, { x: number; y: number }> = {}
    for (const n of graph.nodes) {
      if (typeof n.x === 'number' && typeof n.y === 'number') {
        map[n.id] = { x: n.x, y: n.y }
      } else {
        const legacy = legacyPositionsRef.current[n.id]
        if (legacy) map[n.id] = legacy
      }
    }
    return map
  }, [graph.nodes])

  /** 拖节点：直接改节点 x/y（随暂存 / 提交一起持久化）。 */
  const moveNode = useCallback((id: string, x: number, y: number) => {
    setGraph((g) => ({
      ...g,
      nodes: g.nodes.map((n) => (n.id === id ? { ...n, x, y } : n)),
    }))
  }, [])

  /** 拉节点目录：面板 / 端口 / 配置字段都按它渲染（只读后端内存里那张注册表，不碰库）。 */
  const loadCatalog = useCallback(async () => {
    setCatalogFailed(false)
    try {
      const { data } = await fetchNodeCatalog()
      setPalette(installCatalog(data.nodes))
    } catch {
      setCatalogFailed(true)
    }
  }, [])

  useEffect(() => {
    void loadCatalog()
  }, [loadCatalog])

  useEffect(() => {
    if (!workflowId) return
    let cancelled = false
    void (async () => {
      setLoading(true)
      try {
        // 定义里的 current_ref 决定打开时看暂存区还是已提交版本
        const { data: def } = await getWorkflow(workflowId)
        const { data: vers } = await listVersions(workflowId)
        if (cancelled) return
        setDefinition(def)
        setVersions(vers)

        let loaded: WorkflowGraph | null = null
        if (def.current_ref === 'draft') {
          const { data: draft } = await getDraft(workflowId)
          if (draft.graph) loaded = draft.graph
        }
        // 指针指向版本 / 暂存区为空但已有提交：读对应版本快照（缺省读最新版）
        if (!loaded && def.current_version > 0) {
          const wantVersion =
            def.current_ref === 'version' ? def.current_version : vers[0]?.version
          if (wantVersion) {
            const { data: snapshot } = await getVersion(workflowId, wantVersion)
            loaded = snapshot.graph
          }
        }
        if (!cancelled && loaded) {
          setGraph(withLegacyPositions(normalizeGraph(loaded), legacyPositionsRef.current))
        }
      } catch (err) {
        pushToast('error', err instanceof ApiRequestError ? err.message : '加载失败')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [workflowId, pushToast])

  // ---- 节点操作 ----
  const addNode = useCallback(
    (type: string) => {
      const def = nodeDef(type)
      const id = uid(type)
      const node: WorkflowNode = {
        id,
        type: def.type,
        config: { ...def.defaults },
        outputs: [],
        x: 80 + Math.random() * 200,
        y: 80 + Math.random() * 120,
      }
      setGraph((g) => ({ ...g, nodes: [...g.nodes, node] }))
      setSelectedId(id)
    },
    [],
  )

  const deleteNode = useCallback(
    (id: string) => {
      setGraph((g) => ({
        nodes: g.nodes.filter((n) => n.id !== id),
        edges: g.edges.filter((e) => e.source !== id && e.target !== id),
      }))
      if (selectedId === id) setSelectedId(null)
    },
    [selectedId],
  )

  const deleteSelected = useCallback(() => {
    if (selectedIds.size === 0) return
    setGraph((g) => ({
      nodes: g.nodes.filter((n) => !selectedIds.has(n.id)),
      edges: g.edges.filter((e) => !selectedIds.has(e.source) && !selectedIds.has(e.target)),
    }))
    setSelectedIds(new Set())
  }, [selectedIds])

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

  // ---- 常量节点：一行一个「名字 -> 值」，名字同步进 outputs（下游 {{名字}} 引用靠它）----
  const addConstant = useCallback((id: string) => {
    setGraph((g) => ({
      ...g,
      nodes: g.nodes.map((n) => {
        if (n.id !== id) return n
        let index = 1
        while (`name_${index}` in n.config) index += 1
        const config = { ...n.config, [`name_${index}`]: '' }
        return { ...n, config, outputs: Object.keys(config) }
      }),
    }))
  }, [])

  const renameConstant = useCallback((id: string, from: string, to: string) => {
    setGraph((g) => ({
      ...g,
      nodes: g.nodes.map((n) => {
        if (n.id !== id) return n
        const config: Record<string, unknown> = {}
        for (const [key, value] of Object.entries(n.config)) {
          config[key === from ? to : key] = value
        }
        return { ...n, config, outputs: Object.keys(config) }
      }),
    }))
  }, [])

  const removeConstant = useCallback((id: string, name: string) => {
    setGraph((g) => ({
      ...g,
      nodes: g.nodes.map((n) => {
        if (n.id !== id) return n
        const config: Record<string, unknown> = {}
        for (const [key, value] of Object.entries(n.config)) {
          if (key !== name) config[key] = value
        }
        return { ...n, config, outputs: Object.keys(config) }
      }),
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
      boxMovedRef.current = true
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
      moveNode(nodeId, x - offsetX, y - offsetY)
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
    // 真正拖出过框选：松手后浏览器会补发一发 click，先立牌子让 onClick 跳过清空，
    // 否则刚框选中的节点会被它故意清掉（普通点击不立牌子——那发 click 正是取消选中要用的）
    if (boxRef.current && boxMovedRef.current) suppressClickRef.current = true
    dragRef.current = null
    connectRef.current = null
    panRef.current = null
    boxRef.current = null
    boxMovedRef.current = false
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

  // ---- 暂存 / 校验 / 提交版本 / 发布 ----
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

  /** 暂存：不校验，编辑到一半也能存；存完指针留在暂存区。 */
  const onDraft = useCallback(async () => {
    if (!workflowId) return
    setDrafting(true)
    try {
      const { data } = await saveDraft(workflowId, graph)
      setDefinition(data)
      pushToast('success', '已暂存')
    } catch (err) {
      pushToast('error', err instanceof ApiRequestError ? err.message : '暂存失败')
    } finally {
      setDrafting(false)
    }
  }, [workflowId, graph, pushToast])

  /** 提交版本：先校验后写不可变快照，成功后指针切到版本侧。 */
  const onSave = useCallback(async () => {
    if (!workflowId) return
    setSaving(true)
    try {
      const { data } = await saveVersion(workflowId, graph, '画布编辑')
      // 校验不过：后端返回 200 + 校验报告（没有 version/created），不写库
      if ('valid' in data) {
        setReport(data)
        pushToast('error', `校验未通过（${data.stage}），未提交，请修正后重试`)
        return
      }
      const result: SaveVersionResultData = data
      if (!result.created) {
        pushToast('info', '内容未变，未产生新版本')
      } else {
        pushToast('success', `已提交 v${result.version.version}`)
      }
      setDefinition(result.workflow)
      setVersions((v) => [result.version, ...v.filter((x) => x.version !== result.version.version)])
      setReport(null)
    } catch (err) {
      pushToast('error', err instanceof ApiRequestError ? err.message : '提交失败')
    } finally {
      setSaving(false)
    }
  }, [workflowId, graph, pushToast])

  /**
   * 拨**运行开关**：发布 ≠ 运行 —— 拨开才真的按已发布版本跑（默认关）。
   *
   * 还没发布过就点它是没意义的（后端也会 409），所以按钮在那种情况下是禁用的。
   */
  const onToggleEnabled = useCallback(async () => {
    if (!definition) return
    const next = !definition.enabled
    setSwitching(true)
    try {
      const { data } = await setWorkflowEnabled(definition.id, next)
      setDefinition(data)
      pushToast(
        'success',
        next ? `已开启：按已发布版本 v${data.published_version} 跑` : '已停止：不再定时触发',
      )
    } catch (err) {
      pushToast('error', err instanceof ApiRequestError ? err.message : '开关切换失败')
    } finally {
      setSwitching(false)
    }
  }, [definition, pushToast])

  const onPublish = useCallback(async () => {
    if (!workflowId) return
    try {
      const { data } = await publishWorkflow(workflowId)
      setDefinition(data)
      pushToast('success', '最新版本已发布（重启服务后生效）')
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
          {definition?.current_ref === 'draft' ? (
            <span className={styles.verTag} title="当前查看 / 编辑的是暂存区">
              暂存区{definition.draft_updated_at ? '（已暂存）' : ''}
            </span>
          ) : (
            definition && definition.current_version > 0 && (
              <span className={styles.verTag} title="当前查看的是已提交版本">
                当前 v{definition.current_version}
              </span>
            )
          )}
        </div>
        <div className={styles.toolbarActions}>
          <button className="btn" onClick={() => void onDraft()} disabled={drafting}>
            <IconSave size={14} />
            {drafting ? '暂存中…' : '暂存'}
          </button>
          <button className="btn" onClick={() => void onValidate()} disabled={validating}>
            <IconCheck size={14} />
            {validating ? '校验中…' : '校验'}
          </button>
          <button className={`btn ${styles.primary}`} onClick={() => void onSave()} disabled={saving}>
            <IconSave size={14} />
            {saving ? '提交中…' : '保存版本'}
          </button>
          <button className="btn" onClick={() => void onPublish()}>
            发布
          </button>
          <button
            className={`btn ${definition?.enabled ? styles.primary : ''}`}
            disabled={switching || !definition || definition.published_version === 0}
            title={
              definition?.enabled
                ? '点一下停止：不再定时触发（发布状态不变）'
                : '点一下开启：按已发布版本跑'
            }
            onClick={() => void onToggleEnabled()}
          >
            {switching ? '切换中…' : definition?.enabled ? '运行中' : '已停止'}
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
        {palette !== null && showPalette && (
        <aside className={styles.palette}>
          <div className={styles.paletteTitle}>节点</div>
          {palette.map((spec) => {
            const def = nodeDef(spec.type)
            return (
              <button
                key={spec.type}
                className={styles.paletteItem}
                onClick={() => addNode(spec.type)}
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
            // 框选刚结束的那发补发 click 不算「点空白」：留着刚框中的选中
            if (suppressClickRef.current) {
              suppressClickRef.current = false
              return
            }
            setSelectedId(null)
            setSelectedIds(new Set())
          }}
        >
          {palette === null || loading ? (
            <div className={styles.loading}>
              {palette !== null ? (
                <>
                  <span className="spinner" />
                  正在加载…
                </>
              ) : catalogFailed ? (
                <>
                  节点类型加载失败
                  <button className="btn" onClick={() => void loadCatalog()}>
                    重试
                  </button>
                </>
              ) : (
                <>
                  <span className="spinner" />
                  正在加载节点类型…
                </>
              )}
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
                      <path
                        d={edgeCurve(c.x1, c.y1, c.x2, c.y2)}
                        fill="none"
                        stroke={color}
                        strokeWidth="2"
                        strokeLinecap="round"
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
                    <path
                      d={edgeCurve(start.x, start.y, connectCursor.x, connectCursor.y)}
                      fill="none"
                      stroke={color}
                      strokeWidth="2"
                      strokeDasharray="6 5"
                      strokeLinecap="round"
                    />
                  )
                })()}
              </svg>

              {graph.nodes.map((node) => {
                const def = nodeDef(node.type, node.config)
                const pos = positions[node.id] ?? { x: 0, y: 0 }
                const hasError = errorByNode.has(node.id)
                const portRows = Math.max(def.inputs.length, def.outputs.length)
                return (
                  <div
                    key={node.id}
                    className={`${styles.node} ${selectedId === node.id ? styles.selected : ''} ${selectedIds.has(node.id) ? styles.boxSelected : ''} ${hasError ? styles.hasError : ''}`}
                    style={{ left: pos.x, top: pos.y, width: NODE_W, '--c': def.color } as React.CSSProperties}
                    onMouseDown={(e) => onNodeMouseDown(e, node.id)}
                    onClick={(e) => e.stopPropagation()}
                  >
                    {/* 头部：色条 + 标签 */}
                    <div className={styles.nodeHeader}>
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
                    style={{ left: -5, background: PORT_COLORS[inp.type] }}
                    onMouseDown={(e) => onPortMouseDown(e, node.id, inp.id, inp.type, 'in')}
                    onMouseUp={(e) => onPortMouseUp(e, node.id, inp.id, inp.type, 'in')}
                  />
                  <span className={styles.portLabel}>{inp.label}</span>
                </>
              )}
                            </div>
                            {/* 输出端口（右侧） */}
                            <div className={styles.portSideRight}>
              {out && (
                <>
                  <span className={styles.portLabel}>{out.label}</span>
                  <span
                    data-role="port"
                    className={styles.portCircle}
                    style={{ right: -5, background: PORT_COLORS[out.type] }}
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
        {palette !== null && showInspector && (
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
                    {triggerOptionsOf().map((option) => (
                      <option key={option} value={option}>
                        {TRIGGER_LABELS[option] ?? option}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              {selectedNode.type === 'constant' && (
                <div className={styles.field}>
                  <label className={styles.label}>常量（名字 → 值）</label>
                  {Object.entries(selectedNode.config).map(([name, value], index) => (
                    <div className={styles.constRow} key={index}>
                      <input
                        className={styles.input}
                        value={name}
                        placeholder="名字"
                        onChange={(e) => renameConstant(selectedNode.id, name, e.target.value)}
                      />
                      <input
                        className={styles.input}
                        value={String(value ?? '')}
                        placeholder="值"
                        onChange={(e) => updateConfig(selectedNode.id, name, e.target.value)}
                      />
                      <button
                        type="button"
                        className={styles.iconBtn}
                        aria-label={`删除常量 ${name}`}
                        onClick={() => removeConstant(selectedNode.id, name)}
                      >
                        ×
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    className={styles.ghostBtn}
                    onClick={() => addConstant(selectedNode.id)}
                  >
                    + 加一个常量
                  </button>
                  <div className={styles.constHint}>
                    下游写 {'{{名字}}'} 读取；要连了线才读得到。名字会同步进「输出变量」
                  </div>
                </div>
              )}
              {/* 字段清单来自后端目录（中文名与下拉选项都在里面）；端口同名字段走端口交互，不列这里 */}
              {selectedNode.type !== 'constant' &&
                selectedDef.fields
                  .filter((field) => !hasDedicatedEditor(selectedNode.type, field.name))
                  .map((field) => (
                  <div className={styles.field} key={field.name}>
                    <label className={styles.label}>{field.label}</label>
                    {field.options ? (
                      <select
                        className={styles.input}
                        value={String(selectedNode.config[field.name] ?? '')}
                        onChange={(e) => updateConfig(selectedNode.id, field.name, e.target.value)}
                      >
                        {field.options.map((option) => (
                          <option key={option} value={option}>
                            {option}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input
                        className={styles.input}
                        value={String(selectedNode.config[field.name] ?? '')}
                        onChange={(e) => updateConfig(selectedNode.id, field.name, e.target.value)}
                      />
                    )}
                  </div>
                ))}
              {/* 后端没声明的键（旧数据 / 手写图）：照旧给个输入框，别让它在界面上消失 */}
              {selectedNode.type !== 'constant' &&
                Object.keys(selectedNode.config)
                  .filter(
                    (key) =>
                      !hasDedicatedEditor(selectedNode.type, key) &&
                      !selectedDef.fields.some((f) => f.name === key),
                  )
                  .map((key) => (
                    <div className={styles.field} key={`extra-${key}`}>
                      <label className={styles.label}>{key}</label>
                      <input
                        className={styles.input}
                        value={String(selectedNode.config[key] ?? '')}
                        onChange={(e) => updateConfig(selectedNode.id, key, e.target.value)}
                      />
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
