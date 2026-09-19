/**
 * WS 调试页的报文模板（OneBot v11 反向 WS）：选一条填进发送框，可改可直发。
 * 形状对齐 nacho/onebot/models.py（extra=allow，多带字段不会报错）。
 */

export interface WsPreset {
  /** 模板 id（下拉框 value；"custom" 是保留值 = 自定义，不覆盖已编辑内容） */
  id: string
  /** 下拉框文案 */
  label: string
  /** 一句话说明这条报文是干嘛的 */
  hint: string
  /** 生成报文（time 等运行时字段在这里补） */
  build: () => Record<string, unknown>
}

/** 示例机器人号（调试用假 QQ 号） */
const DEMO_SELF_ID = 10000

function now(): number {
  return Math.floor(Date.now() / 1000)
}

export const WS_PRESETS: WsPreset[] = [
  {
    id: 'lifecycle',
    label: '生命周期 · connect',
    hint: 'OneBot 实现连上后先报到的元事件',
    build: () => ({
      time: now(),
      self_id: DEMO_SELF_ID,
      post_type: 'meta_event',
      meta_event_type: 'lifecycle',
      sub_type: 'connect',
    }),
  },
  {
    id: 'heartbeat',
    label: '心跳 · heartbeat',
    hint: '定时心跳：服务端只记日志，不打扰业务',
    build: () => ({
      time: now(),
      self_id: DEMO_SELF_ID,
      post_type: 'meta_event',
      meta_event_type: 'heartbeat',
      interval: 5000,
    }),
  },
  {
    id: 'private',
    label: '私聊消息 · 文本',
    hint: '演示回声会下发 send_msg 动作（接收区能看到）',
    build: () => ({
      time: now(),
      self_id: DEMO_SELF_ID,
      post_type: 'message',
      message_type: 'private',
      sub_type: 'friend',
      message_id: 1001,
      user_id: 123456789,
      message: '你好，机器人',
      raw_message: '你好，机器人',
      sender: { user_id: 123456789, nickname: '调试员', card: '' },
    }),
  },
  {
    id: 'group',
    label: '群聊消息 · 文本',
    hint: '带 group_id 的群消息，回声回进群里',
    build: () => ({
      time: now(),
      self_id: DEMO_SELF_ID,
      post_type: 'message',
      message_type: 'group',
      sub_type: 'normal',
      message_id: 1002,
      group_id: 987654321,
      user_id: 123456789,
      message: '在吗？',
      raw_message: '在吗？',
      sender: { user_id: 123456789, nickname: '调试员', card: '调试员' },
    }),
  },
  {
    id: 'friend-request',
    label: '好友请求 · request',
    hint: '加好友请求事件（flag 用来后续处理）',
    build: () => ({
      time: now(),
      self_id: DEMO_SELF_ID,
      post_type: 'request',
      request_type: 'friend',
      user_id: 223344556,
      comment: '我是调试员',
      flag: 'debug-friend-001',
    }),
  },
  {
    id: 'group-request',
    label: '加群请求 · request',
    hint: '进群申请事件',
    build: () => ({
      time: now(),
      self_id: DEMO_SELF_ID,
      post_type: 'request',
      request_type: 'group',
      sub_type: 'add',
      group_id: 987654321,
      user_id: 223344556,
      comment: '申请入群',
      flag: 'debug-group-002',
    }),
  },
  {
    id: 'group-increase',
    label: '群成员增加 · notice',
    hint: '有人进群的通知事件',
    build: () => ({
      time: now(),
      self_id: DEMO_SELF_ID,
      post_type: 'notice',
      notice_type: 'group_increase',
      group_id: 987654321,
      user_id: 223344556,
      operator_id: 223344556,
    }),
  },
  {
    id: 'poke',
    label: '戳一戳 · notice',
    hint: 'go-cqhttp 风格的 notify poke',
    build: () => ({
      time: now(),
      self_id: DEMO_SELF_ID,
      post_type: 'notice',
      notice_type: 'notify',
      sub_type: 'poke',
      group_id: 987654321,
      user_id: 123456789,
      target_id: DEMO_SELF_ID,
    }),
  },
  {
    id: 'action-ok',
    label: '动作回应 · ok',
    hint: '回应框架下发的动作：echo 要照抄，conn.call 才等得到',
    build: () => ({ status: 'ok', retcode: 0, data: null, echo: '把动作里的 echo 抄到这里' }),
  },
]
