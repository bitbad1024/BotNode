/**
 * 拖节点：按住框选组里的节点 = 整组一起挪，每个成员的相对布局保持不变。
 *
 * 拖动期间**不写撤销栈**：一次拖动只算一步，松手时才把「拖动前」那份快照压进去
 * （见 :meth:`NodeDrag.finish`）。位置本身是随图一起持久化的，所以每帧都要写回 graph。
 *
 * 依赖（``moveNodes`` / ``bringToFront`` / ``pushUndo`` / 两个 setSelected）以对象传入并
 * 存进 ref：本 hook 返回的回调因此**身份稳定**，不会因为在渲染里重建而让 memo 化的节点
 * 卡片白失效一次。
 */
import { useCallback, useEffect, useMemo, useRef } from 'react'
import type { Point, Positions, WorkflowGraph } from './catalog'

export interface NodeDragDeps {
  moveNodes: (next: Positions) => void
  bringToFront: (ids: Iterable<string>) => void
  pushUndo: (snapshot?: WorkflowGraph, coalesceKey?: string) => void
  setSelectedId: (id: string | null) => void
  setSelectedIds: (ids: Set<string>) => void
}

export interface DragStartParams {
  nodeId: string
  /** 按下时指针的**画布**坐标 */
  canvasPoint: Point
  positions: Positions
  selectedIds: Set<string>
  /** 拖动前的整图快照（松手时按它记一步撤销） */
  graph: WorkflowGraph
}

export interface NodeDrag {
  start: (params: DragStartParams) => void
  /** 指针移到新的画布坐标：整组平移（没越阈值就不算拖动） */
  move: (point: Point) => void
  /** 松手：真拖动过就记一步撤销，返回是否拖动过 */
  finish: () => boolean
  /** 当前是不是正在拖（画布鼠标事件按它分流） */
  active: () => boolean
}

export function useNodeDrag(deps: NodeDragDeps): NodeDrag {
  const depsRef = useRef(deps)
  useEffect(() => {
    depsRef.current = deps
  })

  const dragRef = useRef<{
    nodeId: string
    /** 指针相对主节点左上角的偏移 */
    offsetX: number
    offsetY: number
    /** 整组（含主节点）的起始坐标快照 */
    starts: Positions
    moved: boolean
  } | null>(null)
  const undoRef = useRef<WorkflowGraph | null>(null)

  const start = useCallback(({ nodeId, canvasPoint, positions, selectedIds, graph }: DragStartParams) => {
    const pos = positions[nodeId] ?? { x: 0, y: 0 }
    // 按住的节点在框选集合里 → 整组一起拖；不在则框选让位，只拖它自己
    const group = selectedIds.has(nodeId) ? [...selectedIds] : [nodeId]
    if (selectedIds.size > 0 && !selectedIds.has(nodeId)) depsRef.current.setSelectedIds(new Set())
    const starts: Positions = {}
    for (const id of group) starts[id] = positions[id] ?? { x: 0, y: 0 }
    dragRef.current = {
      nodeId,
      offsetX: canvasPoint.x - pos.x,
      offsetY: canvasPoint.y - pos.y,
      starts,
      moved: false,
    }
    undoRef.current = graph
    depsRef.current.setSelectedId(nodeId)
    // 图层固化：按住的这组提到数组末尾（松手 / 取消选中后不再落回原层）
    depsRef.current.bringToFront(group)
  }, [])

  const move = useCallback((point: Point) => {
    const drag = dragRef.current
    if (!drag) return
    const { nodeId, offsetX, offsetY, starts } = drag
    // 以主节点的位移为准，整组同步平移（各成员相对布局保持不变）
    const anchor = starts[nodeId] ?? { x: 0, y: 0 }
    const dx = point.x - offsetX - anchor.x
    const dy = point.y - offsetY - anchor.y
    if (dx === 0 && dy === 0) return
    drag.moved = true
    const next: Positions = {}
    for (const [id, s] of Object.entries(starts)) next[id] = { x: s.x + dx, y: s.y + dy }
    depsRef.current.moveNodes(next)
  }, [])

  const finish = useCallback(() => {
    const moved = dragRef.current?.moved ?? false
    if (moved && undoRef.current) depsRef.current.pushUndo(undoRef.current)
    dragRef.current = null
    undoRef.current = null
    return moved
  }, [])

  const active = useCallback(() => dragRef.current !== null, [])

  // 整体 memo：本 hook 的返回值身份固定，传给 memo 化组件不会把它顶掉
  return useMemo(() => ({ start, move, finish, active }), [start, move, finish, active])
}
