/**
 * 虚影卡片：跟鼠标走、不吃鼠标事件的「半张卡」。
 *
 * 两处用它：Ctrl+V 的粘贴虚影（落子前先预览）与从节点库往画布拖的新节点虚影。
 * 只画样子（头部 + 端口），不接线也不响应事件 —— 落子由画布的左键负责。
 */
import { NODE_W, PORT_COLORS, PORT_ROW_H, nodeDef, nodeHeight } from './catalog'
import styles from '../WorkflowEditor.module.css'

export interface GhostNodeProps {
  type: string
  config?: Record<string, unknown>
  left: number
  top: number
}

export function GhostNode({ type, config, left, top }: GhostNodeProps) {
  const def = nodeDef(type, config)
  const portRows = Math.max(def.inputs.length, def.outputs.length)

  return (
    <div
      className={styles.ghostNode}
      style={
        {
          left,
          top,
          width: NODE_W,
          minHeight: nodeHeight(def),
          '--c': def.color,
        } as React.CSSProperties
      }
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
                    <span
                      className={styles.portCircle}
                      style={{ left: -5, background: PORT_COLORS[inp.type] }}
                    />
                    <span className={styles.portLabel}>{inp.label}</span>
                  </>
                )}
              </div>
              <div className={styles.portSideRight}>
                {out && (
                  <>
                    <span className={styles.portLabel}>{out.label}</span>
                    <span
                      className={styles.portCircle}
                      style={{ right: -5, background: PORT_COLORS[out.type] }}
                    />
                  </>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
