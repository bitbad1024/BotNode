/** 侧边栏导航配置（单一数据源，侧栏 / 顶栏标题 / 路由都从这里取）。 */
import type { ComponentType, SVGProps } from 'react'
import {
  IconDashboard,
  IconDevices,
  IconKey,
  IconSchedule,
  IconLogs,
  IconTerminal,
  IconBolt,
} from '../../common/icons'

export interface NavItem {
  /** 路由路径 */
  path: string
  /** 菜单与顶栏标题文案 */
  label: string
  /** 线性图标组件 */
  icon: ComponentType<SVGProps<SVGSVGElement> & { size?: number }>
  /** 根路径需 end 匹配，避免任意页面都高亮“工作台” */
  end?: boolean
}

export const NAV_ITEMS: NavItem[] = [
  { path: '/', label: '工作台', icon: IconDashboard, end: true },
  { path: '/sessions', label: '登录设备', icon: IconDevices },
  { path: '/tokens', label: '令牌管理', icon: IconKey },
  { path: '/workflows', label: '工作流', icon: IconBolt },
  { path: '/schedule', label: '调度计划', icon: IconSchedule },
  { path: '/logs', label: '运行日志', icon: IconLogs },
  { path: '/debug', label: 'WS 调试', icon: IconTerminal },
]
