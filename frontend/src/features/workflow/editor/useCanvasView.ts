/**
 * 画布视图：pan（平移）+ zoom（缩放），以及这两个之间的坐标换算。
 *
 * 只负责「看」的这一层：
 *
 * * ``toCanvas`` —— 屏幕坐标 -> 画布坐标（拖节点 / 框选 / 连线都靠它）；
 * * ``zoomAt``    —— 以鼠标位置为中心缩放（缩放时鼠标底下那个点不动）；
 * * ``reset``     —— 回原位（工具栏那个百分比按钮）。
 *
 * 缩放是 CSS ``zoom``（不是 ``transform: scale``）：按新尺寸重新排版，文字与连线都是矢量
 * 重绘，放到多大都清晰；平移层只做位移，二者拆开互不干扰（见 Canvas 组件）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Point } from './catalog'

export const ZOOM_MIN = 0.25
export const ZOOM_MAX = 3

export function clampZoom(zoom: number): number {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom))
}

export interface CanvasView {
  pan: Point
  zoom: number
  setPan: (pan: Point) => void
  /** 屏幕坐标 -> 画布坐标（rect 是画布可视区的矩形） */
  toCanvas: (clientX: number, clientY: number, rect: DOMRect) => Point
  /** 以可视区内坐标 (mx, my) 为中心缩放到 next（超出范围自动夹紧） */
  zoomAt: (next: number, mx: number, my: number) => void
  reset: () => void
}

export function useCanvasView(): CanvasView {
  const [pan, setPan] = useState<Point>({ x: 0, y: 0 })
  const [zoom, setZoom] = useState(1)
  // 事件里要读到最新一份（wheel 连续触发时 state 还没落地），又不想让回调频繁换身份
  const viewRef = useRef({ pan, zoom })

  useEffect(() => {
    viewRef.current = { pan, zoom }
  }, [pan, zoom])

  const toCanvas = useCallback((clientX: number, clientY: number, rect: DOMRect): Point => {
    const { pan: p, zoom: z } = viewRef.current
    return { x: (clientX - rect.left - p.x) / z, y: (clientY - rect.top - p.y) / z }
  }, [])

  const zoomAt = useCallback((raw: number, mx: number, my: number) => {
    const { pan: p, zoom: z } = viewRef.current
    const next = clampZoom(raw)
    if (next === z) return
    // 以鼠标位置为缩放中心：调整 pan 使鼠标下方的画布点不变
    const ratio = next / z
    setPan({ x: mx - (mx - p.x) * ratio, y: my - (my - p.y) * ratio })
    setZoom(next)
  }, [])

  const reset = useCallback(() => {
    setPan({ x: 0, y: 0 })
    setZoom(1)
  }, [])

  // 整体 memo：回调身份只在 pan / zoom 真的变了才变（否则 memo 化的画布内容会白重渲染）
  return useMemo(
    () => ({ pan, zoom, setPan, toCanvas, zoomAt, reset }),
    [pan, zoom, setPan, toCanvas, zoomAt, reset],
  )
}
