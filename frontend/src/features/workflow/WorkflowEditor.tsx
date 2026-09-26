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

//: 边没写端口时的口径：按「触发 -> 触发」读（与后端 graph.DEFAULT_EDGE_PORT 一致）
const DEFAULT_PORT = 'trigger'

//: 节点配色（皮肤）：后端只给类型名与显示名，颜色由这里定
const NODE_COLORS: Record<string, string> = {
  start: '#22c55e',
  end: '#ef4444',
  log: '#3b82f6',
  test: '#8b5cf6',
  http: '#0ea5e9',
  constant: '#eab308',
  delay: '#14b8a6',
  json: '#f97316',
  regex: '#ec4899',
  now: '#84cc16',
  condition: '#6366f1',
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
  /** 卡片底部的字段条：后端声明的字段里，名字**不是**端口的那些（手填值，照实显示） */
  constants: string[]
  /** 配置面板照它渲染（含与端口同名的字段：那是「没接线时的手填兜底」） */
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
const UNKNOWN_PORTS: PortSpec[] = [
  { id: 'trigger', type: 'trigger', label: '触发', required: false },
]

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
    // 卡片底部的字段条：与端口同名的字段（log 的 message）是**数据入口**，值从线上来，
    // 卡片上不重复显示；其余字段（level / method / timeout / value…）是手填值，照实显示
    constants: spec.fields
      .filter((field) => !portIds.has(field.name))
      .map((field) => field.name),
    // 配置面板**照单全收**：与端口同名的字段也要能填 —— 那是「没接线时的手填兜底」
    fields: [...spec.fields],
  }

  if (spec.type === 'start') {
    // 例外：时间形态只出触发端口，卡片上只显示 cron（消息形态两者都不显示）
    if (config?.trigger !== 'time') return { ...base, constants: [] }
    return {
      ...base,
      label: `${base.label} · 时间`,
      color: START_TIME_COLOR,
      outputs: base.outputs.filter((port) => port.id === 'trigger'),
      constants: ['cron'],
    }
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

/** 焦点在输入框 / 下拉 / 可编辑元素里时：画布快捷键要让位给文本编辑 */
function isEditingTarget(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null
  if (!el) return false
  return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable
}

/** 撤销栈上限（步数）：超了从最老的丢 */
const UNDO_LIMIT = 100
/** 连续修改同一字段（打字）的合并窗口（毫秒）：窗内的连续输入合并成一步 */
const UNDO_COALESCE_MS = 800

function emptyGraph(): WorkflowGraph {
  return { nodes: [], edges: [] }
}

/**
 * 后端形态归一：边的端口字段后端是 source_port/target_port，统一成前端用的驼峰写法。
 *
 * 端口留空不在这里补：后端按 ``trigger`` 读（只表达先后的边），画布照同一口径显示 ——
 * 见 :data:`DEFAULT_PORT`。
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
  /** 节点右键菜单：视口坐标 + 这一次要操作的节点集合 */
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; ids: string[] } | null>(null)
  /**
   * 粘贴虚影（Ctrl+V 放置模式）：剪贴板内容先以半透明预览跟鼠标走，左键落子才真正放图。
   * x/y = 虚影组中心当前所在画布坐标；cx/cy = 组中心在剪贴板坐标里的位置。
   */
  const [placing, setPlacing] = useState<{
    nodes: WorkflowNode[]
    edges: WorkflowEdge[]
    cx: number
    cy: number
    x: number
    y: number
  } | null>(null)
  /** 节点库拖出的新节点虚影：x/y = 鼠标的画布坐标（null = 没拖 / 不在画布上） */
  const [newDrag, setNewDrag] = useState<{ type: string; x: number; y: number } | null>(null)
  /**
   * 节点类型目录（后端给的）：拉回来之前**不渲染画布** —— 认不出类型就画不出端口。
   * 失败也不退回一份前端定义（那正是以前漂移的来源），只给一个重试。
   */
  const [palette, setPalette] = useState<NodeTypeSpec[] | null>(null)
  const [catalogFailed, setCatalogFailed] = useState(false)

  const canvasRef = useRef<HTMLDivElement>(null)
  /** 拖节点：offset 是指针相对主节点左上角的偏移；starts 是整组（含主节点）的起始坐标快照 */
  const dragRef = useRef<{
    nodeId: string
    offsetX: number
    offsetY: number
    starts: Record<string, { x: number; y: number }>
    /** 本次拖动指针真的动过（松手时决定要不要记撤销点） */
    moved: boolean
  } | null>(null)
  /** 本次拖动开始前的图快照（松手时若真拖动了，按它盖一个撤销点） */
  const dragUndoRef = useRef<WorkflowGraph | null>(null)
  /** 最近一次画布鼠标位置（画布坐标）：Ctrl+V 进入放置模式时拿它当虚影落点 */
  const lastPointerRef = useRef<{ x: number; y: number } | null>(null)
  const panRef = useRef<{ startX: number; startY: number; panX: number; panY: number } | null>(null)
  const boxRef = useRef<{ startX: number; startY: number } | null>(null)
  /** 本次空白拖拽是否已越过阈值进入框选（松手时区分「点了一下」与「框选完」） */
  const boxMovedRef = useRef(false)
  /** 框选结束的松手会被浏览器补发一发 click，用它立牌子吞掉（见 onCanvasMouseUp） */
  const suppressClickRef = useRef(false)
  const menuRef = useRef<HTMLDivElement>(null)
  /** 本次右键是否真的拖动过画布（拖过就不弹节点右键菜单） */
  const panMovedRef = useRef(false)
  /** 画布内部剪贴板：Ctrl+C / Ctrl+X 存这里的节点 + 组内连线，Ctrl+V 以虚影放置 */
  const clipboardRef = useRef<{ nodes: WorkflowNode[]; edges: WorkflowEdge[] } | null>(null)
  /** 撤销栈（Ctrl+Z）：每个可撤销操作开始前存一份图快照，弹回上一份 */
  const undoStackRef = useRef<WorkflowGraph[]>([])
  /** 连续修改同一字段（打字）的合并标记：同 key + 时间窗内不重复压栈 */
  const lastUndoRef = useRef<{ key: string; at: number } | null>(null)
  /** 正在拖出的连线：起点端口信息 + 鼠标位置 */
  const connectRef = useRef<{
    nodeId: string
    portId: string
    portType: PortType
    direction: 'in' | 'out'
  } | null>(null)
  const [connectCursor, setConnectCursor] = useState<{ x: number; y: number } | null>(null)

  /** 坐标直接从节点 x/y 派生（渲染 / 框选 / 连线都读它）；没存过坐标的节点落在原点。 */
  const positions = useMemo(() => {
    const map: Record<string, { x: number; y: number }> = {}
    for (const n of graph.nodes) {
      map[n.id] = {
        x: typeof n.x === 'number' ? n.x : 0,
        y: typeof n.y === 'number' ? n.y : 0,
      }
    }
    return map
  }, [graph.nodes])

  /** 拖节点（一次可挪一批，组拖动用）：直接改节点 x/y（随暂存 / 提交一起持久化）。 */
  const moveNodes = useCallback((next: Record<string, { x: number; y: number }>) => {
    setGraph((g) => ({
      ...g,
      nodes: g.nodes.map((n) => {
        const p = next[n.id]
        return p ? { ...n, x: p.x, y: p.y } : n
      }),
    }))
  }, [])

  /**
   * 提到图层最上：把节点挪到数组末尾（渲染序 = DOM 序 = 图层序）。
   * 不是临时样式——松手 / 取消选中后顺序依然保持，并随暂存一起保存。
   */
  const bringToFront = useCallback((ids: Iterable<string>) => {
    const set = new Set(ids)
    if (set.size === 0) return
    setGraph((g) => {
      const front = g.nodes.filter((n) => set.has(n.id))
      if (front.length === 0) return g
      const rest = g.nodes.filter((n) => !set.has(n.id))
      const next = [...rest, ...front]
      // 本来就在末尾（相对顺序没变）就不动，省一次重渲染
      if (next.every((n, i) => n === g.nodes[i])) return g
      return { ...g, nodes: next }
    })
  }, [])

  // ---- 撤销（Ctrl+Z）----

  /**
   * 记一个撤销点：在「可撤销操作」改图之前调用，存下操作前的整图快照。
   * coalesceKey 相同且在时间窗内：连续打字合并成一步（快照取最初那次的），不逐字符占栈。
   */
  const pushUndo = useCallback(
    (snapshot?: WorkflowGraph, coalesceKey?: string) => {
      const now = Date.now()
      if (coalesceKey) {
        const last = lastUndoRef.current
        if (last && last.key === coalesceKey && now - last.at < UNDO_COALESCE_MS) {
          last.at = now
          return
        }
      }
      const stack = undoStackRef.current
      stack.push(structuredClone(snapshot ?? graph))
      if (stack.length > UNDO_LIMIT) stack.shift()
      lastUndoRef.current = coalesceKey ? { key: coalesceKey, at: now } : null
    },
    [graph],
  )

  /** Ctrl+Z：弹回上一份快照；选中态收敛到快照里仍存在的节点。 */
  const undo = useCallback(() => {
    const snap = undoStackRef.current.pop()
    if (!snap) return
    lastUndoRef.current = null
    setGraph(snap)
    const ids = new Set(snap.nodes.map((n) => n.id))
    setSelectedIds((cur) => {
      const next = new Set([...cur].filter((id) => ids.has(id)))
      return next.size === cur.size ? cur : next
    })
    setSelectedId((cur) => (cur && !ids.has(cur) ? null : cur))
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
          // 打开工作流：撤销栈归零（Ctrl+Z 不会跨工作流回退）
          undoStackRef.current = []
          lastUndoRef.current = null
          setGraph(normalizeGraph(loaded))
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
    (type: string, pos?: { x: number; y: number }) => {
      const def = nodeDef(type)
      const id = uid(type)
      pushUndo()
      const node: WorkflowNode = {
        id,
        type: def.type,
        config: { ...def.defaults },
        // pos：拖拽落点（鼠标位置，按节点中心换算成左上角）；点击添加沿用随机错开位置
        x: pos ? pos.x - NODE_W / 2 : 80 + Math.random() * 200,
        y: pos ? pos.y - nodeHeight(def) / 2 : 80 + Math.random() * 120,
      }
      setGraph((g) => ({ ...g, nodes: [...g.nodes, node] }))
      setSelectedId(id)
    },
    [pushUndo],
  )

  /**
   * 节点库选项按下：拖进画布 → 虚影跟随、松手落子；没拖（纯点击）→ 和以前一样直接添加。
   * 监听挂在 window 上：拖拽路径大半在画布外（左侧栏），画布上的 mousemove 收不到。
   */
  const onPaletteMouseDown = (e: React.MouseEvent, type: string) => {
    if (e.button !== 0) return
    e.preventDefault() // 防文本选中 / 原生拖拽
    const drag = { startX: e.clientX, startY: e.clientY, armed: false }
    /** 鼠标在画布可视区里就返回画布矩形（用于坐标换算），否则 null */
    const insideCanvas = (ev: MouseEvent) => {
      const rect = canvasRef.current?.getBoundingClientRect()
      if (!rect) return null
      if (ev.clientX < rect.left || ev.clientX > rect.right || ev.clientY < rect.top || ev.clientY > rect.bottom) return null
      return rect
    }
    const onMove = (ev: MouseEvent) => {
      if (!drag.armed) {
        // 位移超过阈值才算「拖拽」，没超过就还是「点击」
        if (Math.abs(ev.clientX - drag.startX) + Math.abs(ev.clientY - drag.startY) < 5) return
        drag.armed = true
      }
      const rect = insideCanvas(ev)
      if (!rect) {
        setNewDrag(null) // 不在画布上：虚影收起
        return
      }
      setNewDrag({ type, x: (ev.clientX - rect.left - pan.x) / zoom, y: (ev.clientY - rect.top - pan.y) / zoom })
    }
    const onUp = (ev: MouseEvent) => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      if (!drag.armed) {
        addNode(type) // 纯点击：和以前一样直接出现
        return
      }
      setNewDrag(null)
      const rect = insideCanvas(ev)
      if (!rect) return // 松手在画布外：取消，不添加
      addNode(type, { x: (ev.clientX - rect.left - pan.x) / zoom, y: (ev.clientY - rect.top - pan.y) / zoom })
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  const deleteNode = useCallback(
    (id: string) => {
      pushUndo()
      setGraph((g) => ({
        nodes: g.nodes.filter((n) => n.id !== id),
        edges: g.edges.filter((e) => e.source !== id && e.target !== id),
      }))
      if (selectedId === id) setSelectedId(null)
    },
    [selectedId, pushUndo],
  )

  /** 按 id 批量删除节点（连带两端连线），并清理指向它们的选中态。Delete 键 / 右键菜单共用。 */
  const deleteNodesByIds = useCallback((ids: string[]) => {
    if (ids.length === 0) return
    pushUndo()
    const set = new Set(ids)
    setGraph((g) => ({
      nodes: g.nodes.filter((n) => !set.has(n.id)),
      edges: g.edges.filter((e) => !set.has(e.source) && !set.has(e.target)),
    }))
    setSelectedIds((cur) => {
      const next = new Set([...cur].filter((id) => !set.has(id)))
      return next.size === cur.size ? cur : next
    })
    setSelectedId((cur) => (cur && set.has(cur) ? null : cur))
  }, [pushUndo])

  // 右键菜单：点别处（或按 Esc）关闭
  useEffect(() => {
    if (!ctxMenu) return
    const onDown = (e: MouseEvent) => {
      if (menuRef.current?.contains(e.target as Node)) return
      setCtxMenu(null)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setCtxMenu(null)
    }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [ctxMenu])

  const deleteEdge = useCallback((edge: WorkflowEdge) => {
    pushUndo()
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
  }, [pushUndo])

  const updateConfig = useCallback((id: string, key: string, value: unknown) => {
    // 连续打字合并成一步撤销（同一节点的同一字段）
    pushUndo(undefined, `cfg:${id}:${key}`)
    setGraph((g) => ({
      ...g,
      nodes: g.nodes.map((n) =>
        n.id === id ? { ...n, config: { ...n.config, [key]: value } } : n,
      ),
    }))
  }, [pushUndo])

  /** 切换开始节点的触发方式：time 补默认 cron；message 清掉 cron。 */
  const setStartTrigger = useCallback((id: string, trigger: string) => {
    pushUndo()
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
  }, [pushUndo])

  // ---- 右键菜单 ----
  /** 右键节点：点在框选集合内 = 对整组操作；集合外 = 先让它成为当前选择（只它一个） */
  const openNodeMenu = (e: React.MouseEvent, nodeId: string) => {
    let ids: string[]
    if (selectedIds.has(nodeId)) {
      ids = [...selectedIds]
      setSelectedId(nodeId)
    } else {
      ids = [nodeId]
      setSelectedId(nodeId)
      if (selectedIds.size > 0) setSelectedIds(new Set())
    }
    setCtxMenu({ x: e.clientX, y: e.clientY, ids })
  }

  // ---- 拖拽节点（按住框选组里的节点 = 整组一起挪）----
  const onNodeMouseDown = (e: React.MouseEvent, nodeId: string) => {
    if (e.button !== 0) return // 非左键交给画布处理（右键平移）
    if (placing) return // 放置模式：左键让给画布落子（不 stopPropagation，冒泡上去）
    if ((e.target as HTMLElement).dataset.role === 'port') return
    e.stopPropagation()
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return
    const pos = positions[nodeId] ?? { x: 0, y: 0 }
    // 按住的节点在框选集合里 → 整组一起拖；不在则框选让位，只拖它自己
    const group = selectedIds.has(nodeId) ? [...selectedIds] : [nodeId]
    if (selectedIds.size > 0 && !selectedIds.has(nodeId)) setSelectedIds(new Set())
    const starts: Record<string, { x: number; y: number }> = {}
    for (const id of group) starts[id] = positions[id] ?? { x: 0, y: 0 }
    dragRef.current = {
      nodeId,
      offsetX: (e.clientX - rect.left - pan.x) / zoom - pos.x,
      offsetY: (e.clientY - rect.top - pan.y) / zoom - pos.y,
      starts,
      moved: false,
    }
    // 拖动前的快照：松手时若真拖动了，按它记一个撤销点（一次拖动 = 一步）
    dragUndoRef.current = graph
    setSelectedId(nodeId)
    // 图层固化：按住的这组提到数组末尾（松手 / 取消选中后不再落回原层）
    bringToFront(group)
  }

  const onCanvasMouseDown = (e: React.MouseEvent) => {
    if (e.button === 2) {
      // 右键：开始平移（动没动过留给 panMovedRef 记，松手时决定弹不弹节点菜单）
      e.preventDefault()
      panMovedRef.current = false
      panRef.current = {
        startX: e.clientX,
        startY: e.clientY,
        panX: pan.x,
        panY: pan.y,
      }
    } else if (e.button === 0) {
      if (placing) {
        // 放置模式：这一下左键就是「落子」，不进框选
        e.preventDefault()
        dropPlacing()
        return
      }
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
      if (Math.abs(e.clientX - startX) + Math.abs(e.clientY - startY) > 3) panMovedRef.current = true
      setPan({ x: panX + (e.clientX - startX), y: panY + (e.clientY - startY) })
      return
    }
    const x = (e.clientX - rect.left - pan.x) / zoom
    const y = (e.clientY - rect.top - pan.y) / zoom
    lastPointerRef.current = { x, y }
    if (placing) {
      // 放置模式：虚影组中心跟着鼠标走
      setPlacing({ ...placing, x, y })
      return
    }
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
      const { nodeId, offsetX, offsetY, starts } = dragRef.current
      // 以主节点的位移为准，整组同步平移（各成员相对布局保持不变）
      const anchor = starts[nodeId] ?? { x: 0, y: 0 }
      const dx = x - offsetX - anchor.x
      const dy = y - offsetY - anchor.y
      if (dx !== 0 || dy !== 0) {
        dragRef.current.moved = true
        const next: Record<string, { x: number; y: number }> = {}
        for (const [id, s] of Object.entries(starts)) next[id] = { x: s.x + dx, y: s.y + dy }
        moveNodes(next)
      }
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
    // 真的拖动过节点：松手时把「拖动前」快照记进撤销栈（一次拖动 = 一步）
    const dragUndo = dragUndoRef.current
    if (dragRef.current?.moved && dragUndo) pushUndo(dragUndo)
    dragUndoRef.current = null
    // 真正拖出过框选：松手后浏览器会补发一发 click，先立牌子让 onClick 跳过清空，
    // 否则刚框选中的节点会被它故意清掉（普通点击不立牌子——那发 click 正是取消选中要用的）
    if (boxRef.current && boxMovedRef.current) {
      suppressClickRef.current = true
      // 框选收尾：整组固化到图层末尾（相对顺序保持原样）
      bringToFront(selectedIds)
    }
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
    if (placing) return // 放置模式：端口也让路，左键归画布落子
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
    // 去重：重复连线不占撤销步
    const exists = graph.edges.some(
      (ed) => ed.source === source && ed.target === target && ed.sourcePort === sourcePort && ed.targetPort === targetPort,
    )
    if (!exists) {
      pushUndo()
      setGraph((g) => ({ ...g, edges: [...g.edges, { source, target, sourcePort, targetPort }] }))
    }
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

  // ---- 剪贴板 / 画布快捷键 ----

  /** 当前选择集合：优先框选集合，其次单击选中的那个（删除 / 复制粘贴同一口径）。 */
  const getSelectionIds = useCallback((): Set<string> => {
    if (selectedIds.size > 0) return selectedIds
    return selectedId ? new Set([selectedId]) : new Set<string>()
  }, [selectedIds, selectedId])

  /** 复制选中节点 + 组内连线到内部剪贴板（Ctrl+C / Ctrl+X 共用）；返回复制到的节点数。 */
  const copySelection = useCallback((): number => {
    const ids = getSelectionIds()
    if (ids.size === 0) return 0
    clipboardRef.current = {
      nodes: graph.nodes.filter((n) => ids.has(n.id)).map((n) => structuredClone(n)),
      edges: graph.edges.filter((e) => ids.has(e.source) && ids.has(e.target)),
    }
    return clipboardRef.current.nodes.length
  }, [graph, getSelectionIds])

  /**
   * Ctrl+V：把剪贴板内容挂成虚影进入「放置模式」——虚影组中心跟着鼠标走，
   * 左键落子（dropPlacing）/ Esc 取消。
   */
  const startPlacing = useCallback(() => {
    const clip = clipboardRef.current
    if (!clip || clip.nodes.length === 0) return
    // 先把坐标落到快照上（旧节点用 localStorage 迁来的兜底坐标），渲染 / 落子都直接读
    const base = (n: WorkflowNode) => positions[n.id] ?? { x: n.x ?? 0, y: n.y ?? 0 }
    const nodes = clip.nodes.map((n) => {
      const b = base(n)
      return { ...n, x: b.x, y: b.y }
    })
    // 组包围盒中心：虚影拿它对准鼠标（观感上鼠标「抓着」整组的中腰）
    let minX = Infinity
    let minY = Infinity
    let maxX = -Infinity
    let maxY = -Infinity
    for (const n of nodes) {
      const def = nodeDef(n.type, n.config)
      const nx = n.x ?? 0
      const ny = n.y ?? 0
      minX = Math.min(minX, nx)
      minY = Math.min(minY, ny)
      maxX = Math.max(maxX, nx + NODE_W)
      maxY = Math.max(maxY, ny + nodeHeight(def))
    }
    const cx = (minX + maxX) / 2
    const cy = (minY + maxY) / 2
    // 起点：最近一次画布鼠标位置；没有就退回原位（剪贴板组的中心）
    const start = lastPointerRef.current ?? { x: cx, y: cy }
    setPlacing({ nodes, edges: clip.edges, cx, cy, x: start.x, y: start.y })
  }, [positions])

  /** 落子：按虚影当前所在位置真正放图（副本换新 id、组内连线重建，新节点成为框选集合）。 */
  const dropPlacing = useCallback(() => {
    if (!placing) return
    pushUndo()
    const offX = placing.x - placing.cx
    const offY = placing.y - placing.cy
    const idMap = new Map<string, string>()
    const newNodes = placing.nodes.map((n) => {
      const id = uid(n.type)
      idMap.set(n.id, id)
      return {
        ...n,
        id,
        config: structuredClone(n.config),
        x: (n.x ?? 0) + offX,
        y: (n.y ?? 0) + offY,
      }
    })
    const newEdges = placing.edges.map((e) => ({
      ...e,
      source: idMap.get(e.source) ?? e.source,
      target: idMap.get(e.target) ?? e.target,
    }))
    setGraph((g) => ({ nodes: [...g.nodes, ...newNodes], edges: [...g.edges, ...newEdges] }))
    setSelectedIds(new Set(newNodes.map((n) => n.id)))
    setSelectedId(null)
    setPlacing(null)
    // 落子这一下会带出一发补发 click：立牌子别让它当「点空白」清掉刚选中的新节点
    suppressClickRef.current = true
  }, [placing, pushUndo])

  // 画布快捷键：Delete 删除 / Ctrl+Z 撤销 / Ctrl+S 暂存 / Ctrl+C 复制 / Ctrl+X 剪切 / Ctrl+V 粘贴（先虚影后落子）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.repeat) return
      const mod = e.ctrlKey || e.metaKey
      const key = e.key.toLowerCase()

      // Esc：正在放置的粘贴虚影取消（不落子）
      if (e.key === 'Escape' && placing) {
        setPlacing(null)
        return
      }

      // Ctrl+S 暂存：输入框里也照常生效（先拦掉浏览器默认的「保存网页」）
      if (mod && key === 's') {
        e.preventDefault()
        if (!drafting) void onDraft()
        return
      }

      // 输入框 / 下拉里：退格与文本复制粘贴归它们，不抢
      if (isEditingTarget(e.target)) return

      if (mod && key === 'z' && !e.shiftKey) {
        e.preventDefault()
        // 先收掉挂着的虚影，再撤销上一步
        setPlacing(null)
        undo()
        return
      }

      if (mod && (key === 'c' || key === 'x')) {
        if (copySelection() === 0) return // 没选中什么就不劫持
        e.preventDefault()
        if (key === 'x') deleteNodesByIds([...getSelectionIds()])
        return
      }

      if (mod && key === 'v') {
        if (!clipboardRef.current || clipboardRef.current.nodes.length === 0) return
        e.preventDefault()
        startPlacing()
        return
      }

      if (e.key === 'Delete' || e.key === 'Backspace') {
        const ids = getSelectionIds()
        if (ids.size === 0) return
        e.preventDefault()
        deleteNodesByIds([...ids])
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [drafting, onDraft, undo, copySelection, getSelectionIds, startPlacing, deleteNodesByIds, placing])

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
  /** 选中节点已经接上线的入口（没写端口的边按 trigger 算） */
  const selectedWired = new Set(
    graph.edges
      .filter((e) => e.target === selectedId)
      .map((e) => e.targetPort || DEFAULT_PORT),
  )
  const errorByNode = new Map<string, ValidationIssue[]>()
  if (report) {
    for (const issue of report.errors) {
      const list = errorByNode.get(issue.nodeId) ?? []
      list.push(issue)
      errorByNode.set(issue.nodeId, list)
    }
  }

  /** 计算连线的 SVG 坐标；nodes / posOf 可换成虚影预览的快照节点与偏移后坐标。 */
  function edgeCoords(
    edge: WorkflowEdge,
    nodes: WorkflowNode[] = graph.nodes,
    posOf: (id: string) => { x: number; y: number } | undefined = (id) => positions[id],
  ): { x1: number; y1: number; x2: number; y2: number } | null {
    const srcNode = nodes.find((n) => n.id === edge.source)
    const tgtNode = nodes.find((n) => n.id === edge.target)
    if (!srcNode || !tgtNode) return null
    const sp = posOf(edge.source)
    const tp = posOf(edge.target)
    if (!sp || !tp) return null
    // 没写端口的边按「触发 -> 触发」画（与后端口径一致）
    const sourcePortId = edge.sourcePort || DEFAULT_PORT
    const targetPortId = edge.targetPort || DEFAULT_PORT
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
                onMouseDown={(e) => onPaletteMouseDown(e, spec.type)}
                onClick={(e) => {
                  // 键盘（Enter/Space）触发的 click：detail 为 0；鼠标的交给 mousedown/mouseup 流程
                  if (e.detail === 0) addNode(spec.type)
                }}
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
                //: 这个节点已经接上线的入口（没写端口的边按 trigger 算）
                const wired = new Set(
                  graph.edges
                    .filter((e) => e.target === node.id)
                    .map((e) => e.targetPort || DEFAULT_PORT),
                )
                return (
                  <div
                    key={node.id}
                    className={`${styles.node} ${selectedId === node.id ? styles.selected : ''} ${selectedIds.has(node.id) ? styles.boxSelected : ''} ${hasError ? styles.hasError : ''}`}
                    style={{ left: pos.x, top: pos.y, width: NODE_W, '--c': def.color } as React.CSSProperties}
                    onMouseDown={(e) => onNodeMouseDown(e, node.id)}
                    onClick={(e) => {
                      e.stopPropagation()
                      // 落子虚影带出的补发 click：落在节点上也算消费掉，别留到下次点空白
                      suppressClickRef.current = false
                    }}
                    onContextMenu={(e) => {
                      e.preventDefault()
                      e.stopPropagation()
                      // 右键拖动平移刚结束的那一发：不弹菜单
                      if (panMovedRef.current) return
                      openNodeMenu(e, node.id)
                    }}
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
                    style={{
                      left: -5,
                      background: PORT_COLORS[inp.type],
                      // 必填入口还没接线：红圈提醒（后端也会报 INPUT_NOT_CONNECTED）
                      ...(inp.required && !wired.has(inp.id)
                        ? { boxShadow: '0 0 0 3px rgba(239,68,68,.35)' }
                        : {}),
                    }}
                    title={
                      inp.required
                        ? `${inp.label}（必填入口）：接线，或在配置面板里用同名字段手填`
                        : undefined
                    }
                    onMouseDown={(e) => onPortMouseDown(e, node.id, inp.id, inp.type, 'in')}
                    onMouseUp={(e) => onPortMouseUp(e, node.id, inp.id, inp.type, 'in')}
                  />
                  <span className={styles.portLabel}>
                    {inp.label}
                    {inp.required && !wired.has(inp.id) ? ' *' : ''}
                  </span>
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

              {/* 粘贴虚影（Ctrl+V 放置模式）：组内连线 + 节点预览跟着鼠标走，左键落子 / Esc 取消 */}
              {/* 连线虚影（渲染在虚影节点之前，和真实图一样线在节点下面） */}
              {placing && (() => {
                const offX = placing.x - placing.cx
                const offY = placing.y - placing.cy
                const nodeById = new Map(placing.nodes.map((n) => [n.id, n]))
                /** 虚影节点位置（快照坐标 + 当前偏移）：连线按它算端口坐标 */
                const ghostPosOf = (id: string) => {
                  const n = nodeById.get(id)
                  return n ? { x: (n.x ?? 0) + offX, y: (n.y ?? 0) + offY } : undefined
                }
                return (
                  <svg className={styles.edges} style={{ pointerEvents: 'none' }}>
                    {placing.edges.map((e, i) => {
                      const c = edgeCoords(e, placing.nodes, ghostPosOf)
                      if (!c) return null
                      const srcNode = nodeById.get(e.source)
                      const srcDef = srcNode ? nodeDef(srcNode.type, srcNode.config) : null
                      const port = srcDef?.outputs.find((p) => p.id === (e.sourcePort ?? 'trigger'))
                      const color = port ? PORT_COLORS[port.type] : 'var(--text-3)'
                      return (
                        <path
                          key={i}
                          d={edgeCurve(c.x1, c.y1, c.x2, c.y2)}
                          fill="none"
                          stroke={color}
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeOpacity="0.6"
                        />
                      )
                    })}
                  </svg>
                )
              })()}

              {/* 粘贴虚影节点 */}
              {placing && (() => {
                const offX = placing.x - placing.cx
                const offY = placing.y - placing.cy
                return placing.nodes.map((n) => {
                  const def = nodeDef(n.type, n.config)
                  const portRows = Math.max(def.inputs.length, def.outputs.length)
                  return (
                    <div
                      // 加前缀：复制场景下虚影 id 与图里原节点相同，直接当 key 会撞车
                      key={`ghost-${n.id}`}
                      className={styles.ghostNode}
                      style={{
                        left: (n.x ?? 0) + offX,
                        top: (n.y ?? 0) + offY,
                        width: NODE_W,
                        minHeight: nodeHeight(def),
                        '--c': def.color,
                      } as React.CSSProperties}
                    >
                      <div className={styles.nodeHeader}>
                        <span className={styles.nodeColorBar} style={{ background: def.color }} />
                        <span className={styles.nodeLabel}>{def.label}</span>
                      </div>
                      <div className={styles.ports}>
                        {Array.from({ length: portRows }).map((_, rowIdx) => {
                          const inp = def.inputs[rowIdx]
                          const out = def.outputs[rowIdx]
                          return (
                            <div className={styles.portRow} key={rowIdx} style={{ height: PORT_ROW_H }}>
                              <div className={styles.portSide}>
                                {inp && (
                                  <>
                                    <span className={styles.portCircle} style={{ left: -5, background: PORT_COLORS[inp.type] }} />
                                    <span className={styles.portLabel}>{inp.label}</span>
                                  </>
                                )}
                              </div>
                              <div className={styles.portSideRight}>
                                {out && (
                                  <>
                                    <span className={styles.portLabel}>{out.label}</span>
                                    <span className={styles.portCircle} style={{ right: -5, background: PORT_COLORS[out.type] }} />
                                  </>
                                )}
                              </div>
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  )
                })
              })()}

              {/* 节点库拖出的新节点虚影（中心跟着鼠标；松手在画布上才真正添加） */}
              {newDrag && (() => {
                const def = nodeDef(newDrag.type)
                const portRows = Math.max(def.inputs.length, def.outputs.length)
                const h = nodeHeight(def)
                return (
                  <div
                    className={styles.ghostNode}
                    style={{
                      left: newDrag.x - NODE_W / 2,
                      top: newDrag.y - h / 2,
                      width: NODE_W,
                      minHeight: h,
                      '--c': def.color,
                    } as React.CSSProperties}
                  >
                    <div className={styles.nodeHeader}>
                      <span className={styles.nodeColorBar} style={{ background: def.color }} />
                      <span className={styles.nodeLabel}>{def.label}</span>
                    </div>
                    <div className={styles.ports}>
                      {Array.from({ length: portRows }).map((_, rowIdx) => {
                        const inp = def.inputs[rowIdx]
                        const out = def.outputs[rowIdx]
                        return (
                          <div className={styles.portRow} key={rowIdx} style={{ height: PORT_ROW_H }}>
                            <div className={styles.portSide}>
                              {inp && (
                                <>
                                  <span className={styles.portCircle} style={{ left: -5, background: PORT_COLORS[inp.type] }} />
                                  <span className={styles.portLabel}>{inp.label}</span>
                                </>
                              )}
                            </div>
                            <div className={styles.portSideRight}>
                              {out && (
                                <>
                                  <span className={styles.portLabel}>{out.label}</span>
                                  <span className={styles.portCircle} style={{ right: -5, background: PORT_COLORS[out.type] }} />
                                </>
                              )}
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                )
              })()}

              {boxSel && (() => {
                const x = Math.min(boxSel.x0, boxSel.x1)
                const y = Math.min(boxSel.y0, boxSel.y1)
                const w = Math.abs(boxSel.x1 - boxSel.x0)
                const h = Math.abs(boxSel.y1 - boxSel.y0)
                // 蒙层渲染在节点之后（DOM 序天然在最上），不需要 z-index
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
                    {selectedDef.inputs.map((p) => {
                      const connected = selectedWired.has(p.id)
                      return (
                        <span
                          className={styles.portInfoItem}
                          key={p.id}
                          style={{ color: PORT_COLORS[p.type] }}
                        >
                          ● {p.label}（{p.type}）
                          {p.type === 'message' ? (connected ? ' · 已接线' : ' · 未接线') : ''}
                          {p.required && !connected ? ' · 必填！' : ''}
                        </span>
                      )
                    })}
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
                <div className={styles.constHint}>
                  值沿连线走：上游的 message 输出端口接到本节点的 message 输入端口。
                  带 * 的必填入口没接线时，用下面同名字段手填。
                </div>
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
              {/*
                字段清单来自后端目录（中文名与下拉选项都在里面）。
                与数据入口同名的字段照常可填 —— 那是「没接线时的手填兜底」，标签上写明当前
                这个值是线上来的还是自己填的。
              */}
              {selectedDef.fields
                .filter((field) => !hasDedicatedEditor(selectedNode.type, field.name))
                .map((field) => {
                  const asInput = selectedDef.inputs.find(
                    (p) => p.id === field.name && p.type === 'message',
                  )
                  const fromWire = asInput !== undefined && selectedWired.has(field.name)
                  return (
                    <div className={styles.field} key={field.name}>
                      <label className={styles.label}>
                        {field.label}
                        {asInput &&
                          (fromWire ? '（来自连线，已覆盖）' : '（没接线时手填）')}
                      </label>
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
                  )
                })}
              {/* 后端没声明的键（手写图 / 扩展塞进来的）：照旧给个输入框，别让它在界面上消失 */}
              {Object.keys(selectedNode.config)
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

      {/* 节点右键菜单：fixed 定位（视口坐标），点别处 / Esc 关闭 */}
      {ctxMenu && (
        <div
          ref={menuRef}
          className={styles.ctxMenu}
          style={{
            left: Math.max(8, Math.min(ctxMenu.x, window.innerWidth - 200)),
            top: Math.max(8, Math.min(ctxMenu.y, window.innerHeight - 52)),
          }}
          onContextMenu={(e) => e.preventDefault()}
        >
          <button
            className={`${styles.ctxMenuItem} ${styles.ctxMenuItemDanger}`}
            onClick={() => {
              deleteNodesByIds(ctxMenu.ids)
              setCtxMenu(null)
            }}
          >
            <IconTrash size={14} />
            {ctxMenu.ids.length > 1 ? `删除选中的 ${ctxMenu.ids.length} 个节点` : '删除节点'}
          </button>
        </div>
      )}
    </div>
  )
}
