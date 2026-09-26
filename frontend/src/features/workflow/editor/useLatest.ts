/**
 * 把一份值装进 ref（每次渲染后同步），让**回调身份不变**也能读到最新值。
 *
 * 为什么需要它：画布上的回调要传给 memo 化的节点卡片，回调一换身份，所有卡片都会白重渲染
 * 一遍（拖动时每帧一次，正是要避免的）。于是回调里不直接闭包读 state，改读这里的 ref。
 */
import { useEffect, useRef } from 'react'

export function useLatest<T>(value: T) {
  const ref = useRef(value)
  useEffect(() => {
    ref.current = value
  }, [value])
  return ref
}
