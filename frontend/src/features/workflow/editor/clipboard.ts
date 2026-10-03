/**
 * 画布剪贴板的**载体**：复制时把「节点 + 组内连线 + 复制时刻」打成一份 JSON。
 *
 * 同一份内容写两处（双重保险）：
 *
 * * ``clipboardRef``（内存，见 WorkflowEditor）—— 快，但刷新页面 / 关标签页就没了；
 * * 系统剪贴板（``lib/clipboard``）—— 慢一点，却活得比页面久，还能贴到别的工作流 / 标签页里。
 *
 * 复制时刻（``copiedAt``）也一起写进去：粘贴时能答出「这一份是什么时候拷的」，
 * 从系统剪贴板捞回来的那一份尤其需要 —— 用户早忘了自己拷过什么。
 *
 * 粘贴时两处**都读**（见 ``pickFresherClipboard``）：都读到就比 ``copiedAt`` 用新的那份，
 * 只读到一份就用那一份，两份都没读到就当剪贴板是空的（什么都不做）。
 *
 * 结构带 ``kind`` / ``version``：解析时**只认自己写的那一份**，不认识的（别的软件
 * 放进剪贴板的 JSON、我们以后改了格式的那一份）一律当没有，不往画布上乱贴。
 */
import type { WorkflowEdge, WorkflowNode } from '../workflowApi'

/** 负载标记：系统剪贴板是公共空间，靠它认出「这是画布自己写的」 */
export const CLIP_KIND = 'tickneko.workflow.clip'

/** 负载格式版本：以后改结构就加一，旧版本仍按老口径解析 */
export const CLIP_VERSION = 1

export interface ClipboardPayload {
  kind: typeof CLIP_KIND
  version: number
  /** 复制时刻（Unix 毫秒）：唯一的时间戳，落在负载里跟着内容走 */
  copiedAt: number
  /** 复制的节点（坐标也带上：跨页面恢复时位置就是它） */
  nodes: WorkflowNode[]
  /** 只留**组内**连线（两端都在 nodes 里） */
  edges: WorkflowEdge[]
}

/** 打一份负载；``copiedAt`` 默认取当下，测试 / 重放可以自己传。 */
export function buildClipboardPayload(
  nodes: WorkflowNode[],
  edges: WorkflowEdge[],
  copiedAt: number = Date.now(),
): ClipboardPayload {
  return { kind: CLIP_KIND, version: CLIP_VERSION, copiedAt, nodes, edges }
}

/** 写进系统剪贴板的形态：JSON 文本（换行缩进不要，剪贴板越小越好）。 */
export function serializeClipboard(payload: ClipboardPayload): string {
  return JSON.stringify(payload)
}

/**
 * 把系统剪贴板里的文本解析回负载；不是我们写的那一份就返回 ``null``。
 *
 * 解析**从严**：节点 / 边逐字段挑（只留认识的那几个），带不上线的边直接丢 ——
 * 剪贴板内容可能是别处粘来的、也可能是手改过的，宁可少贴几个节点，不能把画布弄坏。
 */
export function parseClipboard(text: string): ClipboardPayload | null {
  if (!text) return null
  let raw: unknown
  try {
    raw = JSON.parse(text)
  } catch {
    return null // 不是 JSON：普通文本，跟画布无关
  }
  if (typeof raw !== 'object' || raw === null) return null
  const box = raw as Record<string, unknown>
  if (box.kind !== CLIP_KIND) return null
  // 只认不比当前新的格式：以后改结构时，老页面读新负载宁可当没有，也不瞎猜字段
  const version = typeof box.version === 'number' ? box.version : 0
  if (!Number.isFinite(version) || version > CLIP_VERSION) return null

  const nodes = (Array.isArray(box.nodes) ? box.nodes : [])
    .map(pickNode)
    .filter((n): n is WorkflowNode => n !== null)
  if (nodes.length === 0) return null

  const ids = new Set(nodes.map((n) => n.id))
  const edges = (Array.isArray(box.edges) ? box.edges : [])
    .map(pickEdge)
    .filter((e): e is WorkflowEdge => e !== null && ids.has(e.source) && ids.has(e.target))

  return {
    kind: CLIP_KIND,
    version: CLIP_VERSION,
    copiedAt: typeof box.copiedAt === 'number' && Number.isFinite(box.copiedAt) ? box.copiedAt : 0,
    nodes,
    edges,
  }
}

/**
 * 两份负载里挑更新的那一份：只有一份就用它，两份都没有返回 ``null``。
 *
 * 粘贴时内存与系统剪贴板**都读**，所以两边可能都有货 —— 那多半是用户刚在别的标签页 /
 * 别的窗口里复制过，谁新听谁的。``copiedAt`` 相同时（同一发复制写进两处，内容本来就一样）
 * 取内存那一份。
 */
export function pickFresherClipboard(
  local: ClipboardPayload | null,
  system: ClipboardPayload | null,
): ClipboardPayload | null {
  if (!local) return system
  if (!system) return local
  return system.copiedAt > local.copiedAt ? system : local
}

/** 复制时刻的显示口径：今天只看时分秒，跨天补上月日（``0`` = 没带时间戳，返回空串）。 */
export function formatCopiedAt(copiedAt: number): string {
  if (!copiedAt) return ''
  const d = new Date(copiedAt)
  const pad = (n: number) => String(n).padStart(2, '0')
  const hms = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  const sameDay = d.toDateString() === new Date().toDateString()
  return sameDay ? hms : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hms}`
}

/** 挑一个节点：id / type 必须是字符串（其余字段照原样收，config 缺了补空对象）。 */
function pickNode(raw: unknown): WorkflowNode | null {
  if (typeof raw !== 'object' || raw === null) return null
  const box = raw as Record<string, unknown>
  if (typeof box.id !== 'string' || box.id === '') return null
  if (typeof box.type !== 'string' || box.type === '') return null
  const config =
    typeof box.config === 'object' && box.config !== null && !Array.isArray(box.config)
      ? (box.config as Record<string, unknown>)
      : {}
  return {
    id: box.id,
    type: box.type,
    config,
    x: typeof box.x === 'number' ? box.x : null,
    y: typeof box.y === 'number' ? box.y : null,
  }
}

/** 挑一条边：两端必须是字符串；端口留空照原样（后端按 trigger 读）。 */
function pickEdge(raw: unknown): WorkflowEdge | null {
  if (typeof raw !== 'object' || raw === null) return null
  const box = raw as Record<string, unknown>
  if (typeof box.source !== 'string' || box.source === '') return null
  if (typeof box.target !== 'string' || box.target === '') return null
  return {
    source: box.source,
    target: box.target,
    sourcePort: typeof box.sourcePort === 'string' ? box.sourcePort : '',
    targetPort: typeof box.targetPort === 'string' ? box.targetPort : '',
  }
}
