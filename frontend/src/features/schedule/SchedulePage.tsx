/** 调度计划模块（接口接入中），对应后端 core/scheduler 能力。 */
import PlaceholderPage from '../../common/PlaceholderPage'
import { IconSchedule } from '../../common/icons'

export default function SchedulePage() {
  return (
    <PlaceholderPage
      icon={IconSchedule}
      title="调度计划"
      description="基于 cron 表达式编排周期性巡检与维护任务，到时自动触发。"
      endpoint="GET /api/schedules"
      features={[
        '新建 / 编辑 / 暂停 cron 定时任务',
        '时间线视图：查看下一次与最近一次触发时刻',
        '执行历史：成功 / 失败记录与耗时',
        '任务与具体机器人、巡检路线绑定',
      ]}
    />
  )
}
