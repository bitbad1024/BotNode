/** 运行日志模块（接口接入中），对应后端 core/logger 能力。 */
import PlaceholderPage from '../../common/PlaceholderPage'
import { IconLogs } from '../../common/icons'

export default function LogsPage() {
  return (
    <PlaceholderPage
      icon={IconLogs}
      title="运行日志"
      description="集中检索框架运行日志，支持按级别、模块、时间与 trace_id 过滤。"
      endpoint="GET /api/logs"
      features={[
        '级别筛选：DEBUG / INFO / WARNING / ERROR',
        '按模块、关键字与 trace_id 精准检索',
        '时间范围过滤与倒序时间线',
        '单条日志展开查看结构化字段',
      ]}
    />
  )
}
