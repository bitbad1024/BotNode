/**
 * 撤销栈（Ctrl+Z）：每个可撤销操作**改图之前**存一份整图快照，弹回上一份。
 *
 * 为什么整图快照而不是「反向操作」：图上的操作种类多（增删节点 / 连线、改配置、拖动、
 * 粘贴落子），逐个写反向操作既啰嗦又容易漏；图本身不大，直接存快照最稳。
 *
 * 两条约定：
 *
 * * ``pushUndo`` 在**改图之前**调（存的是操作前的样子）；
 * * ``coalesceKey`` 用于「连续打字」：同一节点同一字段在时间窗内的连续输入合并成一步，
 *   不逐字符占栈。
 */
import { useCallback, useEffect, useRef } from 'react'
import type { WorkflowGraph } from './catalog'

/** 撤销栈上限（步数）：超了从最老的丢 */
const UNDO_LIMIT = 100
/** 连续修改同一字段（打字）的合并窗口（毫秒） */
const UNDO_COALESCE_MS = 800

export interface GraphHistory {
  /** 记一个撤销点；``snapshot`` 省略时用**当前图**（调用方还没改它）。 */
  pushUndo: (snapshot?: WorkflowGraph, coalesceKey?: string) => void
  /** 弹回上一份快照（没有就 null）；由调用方真正写回 state。 */
  undo: () => WorkflowGraph | null
  /** 清空（换工作流时：Ctrl+Z 不该跨工作流回退）。 */
  reset: () => void
}

export function useGraphHistory(graph: WorkflowGraph): GraphHistory {
  const stackRef = useRef<WorkflowGraph[]>([])
  const lastRef = useRef<{ key: string; at: number } | null>(null)
  // 省略 snapshot 时要压「当前图」：用 ref 读，pushUndo 才不用每次图变都换一个身份
  const graphRef = useRef(graph)

  useEffect(() => {
    graphRef.current = graph
  }, [graph])

  const pushUndo = useCallback((snapshot?: WorkflowGraph, coalesceKey?: string) => {
    const now = Date.now()
    if (coalesceKey) {
      const last = lastRef.current
      if (last && last.key === coalesceKey && now - last.at < UNDO_COALESCE_MS) {
        last.at = now
        return
      }
    }
    const stack = stackRef.current
    stack.push(structuredClone(snapshot ?? graphRef.current))
    if (stack.length > UNDO_LIMIT) stack.shift()
    lastRef.current = coalesceKey ? { key: coalesceKey, at: now } : null
  }, [])

  const undo = useCallback(() => {
    const snapshot = stackRef.current.pop() ?? null
    lastRef.current = null
    return snapshot
  }, [])

  const reset = useCallback(() => {
    stackRef.current = []
    lastRef.current = null
  }, [])

  return { pushUndo, undo, reset }
}
