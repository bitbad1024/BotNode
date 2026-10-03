/**
 * 画布上的一张节点卡片：头部（色条 + 类型名）+ 端口区 + 常量区 + 校验角标。
 *
 * 用 ``memo`` 包着，且 props 尽量是**可直接比较的标量 / 稳定引用**：
 *
 * * 坐标传 ``x`` / ``y`` 两个数而不是 ``{x,y}`` 对象——拖动时只有被拖的那几个节点数字变了，
 *   其余卡片引用不变、整棵子树跳过重渲染（这是拖动流畅的关键）；
 * * ``wired`` 是「已接线的入口」集合，由父组件按边一次性分组（见
 *   :func:`catalog.wiredPortsByNode`），边的集合没变时身份不变；
 * * 事件回调由父组件 ``useCallback`` 固化。
 */
import { memo } from 'react'
import {
  NODE_W,
  PORT_ROW_H,
  nodeDef,
  portColor,
  portEffKey,
  truncate,
  type PortType,
  type ValidationIssue,
  type WorkflowNode,
} from './catalog'
import styles from '../WorkflowEditor.module.css'

export interface NodeCardProps {
  node: WorkflowNode
  x: number
  y: number
  /** 单击选中 */
  selected: boolean
  /** 框选选中 */
  boxSelected: boolean
  /** 这个节点的校验问题（null = 没问题） */
  issues: ValidationIssue[] | null
  /** 已接上线的入口（没写端口的边按 trigger 算） */
  wired: Set<string>
  /** 端口生效类型表（见 ``catalog.effectivePortTypes``）：泛型端口接什么显什么颜色，
   * 非泛型端口查出来就是它自己；引用由父组件按边的变化用 memo 固化 */
  effTypes: Map<string, string>
  onMouseDown: (event: React.MouseEvent, nodeId: string) => void
  onClick: (event: React.MouseEvent) => void
  onContextMenu: (event: React.MouseEvent, nodeId: string) => void
  onPortMouseDown: (
    event: React.MouseEvent,
    nodeId: string,
    portId: string,
    portType: PortType,
    direction: 'in' | 'out',
  ) => void
  onPortMouseUp: (
    event: React.MouseEvent,
    nodeId: string,
    portId: string,
    portType: PortType,
    direction: 'in' | 'out',
  ) => void
}

function NodeCardBase({
  node,
  x,
  y,
  selected,
  boxSelected,
  issues,
  wired,
  effTypes,
  onMouseDown,
  onClick,
  onContextMenu,
  onPortMouseDown,
  onPortMouseUp,
}: NodeCardProps) {
  const def = nodeDef(node.type, node.config)
  const portRows = Math.max(def.inputs.length, def.outputs.length)

  return (
    <div
      className={`${styles.node} ${selected ? styles.selected : ''} ${boxSelected ? styles.boxSelected : ''} ${issues ? styles.hasError : ''}`}
      style={{ left: x, top: y, width: NODE_W, '--c': def.color } as React.CSSProperties}
      onMouseDown={(e) => onMouseDown(e, node.id)}
      onClick={onClick}
      onContextMenu={(e) => onContextMenu(e, node.id)}
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
          // 端口圆点按「生效类型」显色：泛型端口接什么显什么，声明了 tie 的透传对
          // （输入输出同一种类型）两端颜色永远一致 —— 对应关系就靠这一致的颜色表达
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
                        background: portColor(
                          effTypes.get(portEffKey(node.id, 'in', inp.id)) ?? inp.type,
                        ),
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
                      style={{
                        right: -5,
                        background: portColor(
                          effTypes.get(portEffKey(node.id, 'out', out.id)) ?? out.type,
                        ),
                      }}
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

      {issues && (
        <span
          className={styles.errorBadge}
          title={issues.map((issue) => issue.message).join('\n')}
        >
          {issues.length}
        </span>
      )}
    </div>
  )
}

export const NodeCard = memo(NodeCardBase)
