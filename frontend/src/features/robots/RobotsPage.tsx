/** 机器人模块（接口接入中）。 */
import PlaceholderPage from '../../common/PlaceholderPage'
import { IconRobot } from '../../common/icons'

export default function RobotsPage() {
  return (
    <PlaceholderPage
      icon={IconRobot}
      title="机器人"
      description="纳管巡检机器人的在线状态、心跳与运行任务，统一查看与调度。"
      endpoint="GET /api/robots"
      features={[
        '机器人列表：名称、编号、在线 / 离线状态与最近心跳',
        '单台详情：当前任务、运行参数与历史轨迹',
        '远程下发：唤醒、巡检、回充与急停指令',
        '状态变更实时刷新与异常告警',
      ]}
    />
  )
}
