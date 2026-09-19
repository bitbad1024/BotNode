/**
 * WS 调试页的报文模板（OneBot v11 反向 WS）。
 * 模板是带 {{占位符}} 的 JSON 文本，发送前由 debugStore 统一替换：
 *   {{time}}     发送那一刻的秒级时间戳
 *   {{self}}     机器人 QQ（取最近一次事件里的 self_id，还没有就用 10000）
 *   {{qq}}       发送者 QQ 号（发送区的「QQ 号」输入框）
 *   {{nickname}} 发送者昵称（发送区的「昵称」输入框）
 * 占位符也可以手动改成真实值 —— 替换时匹配不到就不动。
 */

export interface WsPreset {
  /** 模板 id（下拉框 value；"custom" 是保留值 = 自定义，不覆盖已编辑内容） */
  id: string
  /** 下拉框文案 */
  label: string
  /** 一句话说明这条报文是干嘛的 */
  hint: string
  /** 带 {{占位符}} 的 JSON 模板文本 */
  template: string
}

export const WS_PRESETS: WsPreset[] = [
  {
    id: 'lifecycle',
    label: '生命周期 · connect',
    hint: 'OneBot 实现连上后先报到的元事件',
    template: `{
  "time": {{time}},
  "self_id": {{self}},
  "post_type": "meta_event",
  "meta_event_type": "lifecycle",
  "sub_type": "connect"
}`,
  },
  {
    id: 'heartbeat',
    label: '心跳 · heartbeat',
    hint: '定时心跳：服务端只记日志，不打扰业务',
    template: `{
  "time": {{time}},
  "self_id": {{self}},
  "post_type": "meta_event",
  "meta_event_type": "heartbeat",
  "interval": 5000
}`,
  },
  {
    id: 'private',
    label: '私聊消息 · 文本',
    hint: '演示回声会下发 send_msg 动作（预览区机器人气泡）',
    template: `{
  "time": {{time}},
  "self_id": {{self}},
  "post_type": "message",
  "message_type": "private",
  "sub_type": "friend",
  "message_id": 1001,
  "user_id": {{qq}},
  "message": "你好，我是{{nickname}}",
  "raw_message": "你好，我是{{nickname}}",
  "sender": { "user_id": {{qq}}, "nickname": "{{nickname}}", "card": "" }
}`,
  },
  {
    id: 'private-image',
    label: '私聊消息 · 图片',
    hint: '消息段数组：文本 + 图片段，预览区直接把图显示出来',
    template: `{
  "time": {{time}},
  "self_id": {{self}},
  "post_type": "message",
  "message_type": "private",
  "sub_type": "friend",
  "message_id": 1003,
  "user_id": {{qq}},
  "message": [
    { "type": "text", "data": { "text": "看看我的头像 " } },
    { "type": "image", "data": { "file": "https://q1.qlogo.cn/g?b=qq&nk={{qq}}&s=100", "url": "https://q1.qlogo.cn/g?b=qq&nk={{qq}}&s=100" } }
  ],
  "raw_message": "看看我的头像 [CQ:image,file=https://q1.qlogo.cn/g?b=qq&nk={{qq}}&s=100]",
  "sender": { "user_id": {{qq}}, "nickname": "{{nickname}}", "card": "" }
}`,
  },
  {
    id: 'group',
    label: '群聊消息 · 文本',
    hint: '带 group_id 的群消息，回声回进群里',
    template: `{
  "time": {{time}},
  "self_id": {{self}},
  "post_type": "message",
  "message_type": "group",
  "sub_type": "normal",
  "message_id": 1002,
  "group_id": 987654321,
  "user_id": {{qq}},
  "message": "在吗？",
  "raw_message": "在吗？",
  "sender": { "user_id": {{qq}}, "nickname": "{{nickname}}", "card": "{{nickname}}" }
}`,
  },
  {
    id: 'friend-request',
    label: '好友请求 · request',
    hint: '加好友请求事件（flag 用来后续处理）',
    template: `{
  "time": {{time}},
  "self_id": {{self}},
  "post_type": "request",
  "request_type": "friend",
  "user_id": {{qq}},
  "comment": "我是{{nickname}}",
  "flag": "debug-friend-001"
}`,
  },
  {
    id: 'group-request',
    label: '加群请求 · request',
    hint: '进群申请事件',
    template: `{
  "time": {{time}},
  "self_id": {{self}},
  "post_type": "request",
  "request_type": "group",
  "sub_type": "add",
  "group_id": 987654321,
  "user_id": {{qq}},
  "comment": "申请入群",
  "flag": "debug-group-002"
}`,
  },
  {
    id: 'group-increase',
    label: '群成员增加 · notice',
    hint: '有人进群的通知事件',
    template: `{
  "time": {{time}},
  "self_id": {{self}},
  "post_type": "notice",
  "notice_type": "group_increase",
  "group_id": 987654321,
  "user_id": {{qq}},
  "operator_id": {{qq}}
}`,
  },
  {
    id: 'poke',
    label: '戳一戳 · notice',
    hint: 'go-cqhttp 风格的 notify poke',
    template: `{
  "time": {{time}},
  "self_id": {{self}},
  "post_type": "notice",
  "notice_type": "notify",
  "sub_type": "poke",
  "group_id": 987654321,
  "user_id": {{qq}},
  "target_id": {{self}}
}`,
  },
  {
    id: 'action-ok',
    label: '动作回应 · ok',
    hint: '回应框架下发的动作：echo 要照抄，conn.call 才等得到',
    template: `{
  "status": "ok",
  "retcode": 0,
  "data": null,
  "echo": "把动作里的 echo 抄到这里"
}`,
  },
]
