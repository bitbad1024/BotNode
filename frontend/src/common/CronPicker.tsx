/**
 * cron 表达式**可视化选择器**（公共组件，放 ``src/common`` 给各处复用）。
 *
 * 初衷：配「时间触发」要手填五段 cron 串（分 时 日 月 周，比如「每 5 分钟」那种写法），
 * 写错只能等后端报 ``INVALID_CRON`` —— 这里点几下就出表达式，还带一句人话描述
 *（「每 5 分钟」「每周一 09:00」）。
 *
 * 口径与后端一致（``docs/scheduler/scheduler.md``）：5 段「分 时 日 月 周」，也认 6 段
 * （前面多一段秒）；星期 0-6（0 与 7 都是周日）。**组件只生成 5 段**（最通用、够用）：
 * 已存的 6 段值照样能读进来，但秒段不是 ``0`` / ``*`` 时归到「自定义」原样保留 ——
 * 不擅自改用户写过的东西。同理，认不出的写法（限定月份、区间、步长混写）也一律归
 * 「自定义」，原表达式一个字符不动。
 *
 * 组件是**纯受控**的：一切从 ``value`` 解析出来，操作后把新表达式交给 ``onChange``，
 * 自己不留状态（除了没有的状态）—— 外部改了值它立刻跟着变。
 */
import { useMemo, useState } from 'react'
import styles from './CronPicker.module.css'

/** 周期模式：点选出来的几种，覆盖「定时触发」的绝大多数用法 */
export type CronMode =
  | 'everyMinute'
  | 'everyNMinutes'
  | 'hourly'
  | 'daily'
  | 'weekly'
  | 'monthly'
  | 'custom'

export interface CronPickerProps {
  /** 当前表达式（5 / 6 段）；空串 / 认不出 → 按「每分钟」起步 */
  value: string
  /** 点出来的新表达式（5 段；「自定义」模式下是原样的手输文本） */
  onChange: (cron: string) => void
  disabled?: boolean
}

/** 一段 cron 的中间形态：``null`` = 不限（``*``），数字数组 = 指定值，``'bad'`` = 认不出 */
type Field = null | number[] | 'bad'

interface CronDraft {
  mode: CronMode
  every: number
  minute: number
  hour: number
  /** 周几（0-6，0 = 周日） */
  weekDays: number[]
  /** 几号（1-31） */
  monthDays: number[]
  /** 「自定义」模式下的原表达式 */
  raw: string
}

export const WEEK_LABELS = ['日', '一', '二', '三', '四', '五', '六']

const MODE_LABELS: Array<[CronMode, string]> = [
  ['everyMinute', '每分钟'],
  ['everyNMinutes', '每 N 分钟'],
  ['hourly', '每小时'],
  ['daily', '每天'],
  ['weekly', '每周'],
  ['monthly', '每月'],
  ['custom', '自定义（手写表达式）'],
]

/** 「每 N 分钟」的常用步长：够用又不至于让下拉太长 */
const MINUTE_STEPS = [2, 3, 5, 10, 15, 20, 30]

const MINUTES = Array.from({ length: 60 }, (_, i) => i)
const HOURS = Array.from({ length: 24 }, (_, i) => i)
const MONTH_DAYS = Array.from({ length: 31 }, (_, i) => i + 1)

function clamp(n: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, n))
}

function splitFields(expr: string): string[] | null {
  const parts = expr.trim().split(/\s+/).filter(Boolean)
  return parts.length === 5 || parts.length === 6 ? parts : null
}

/** 解析一段：``*`` -> null（不限）；``5`` / ``1,3`` / ``1-5`` -> 数字数组；其它 -> 'bad'。 */
function parseField(part: string): Field {
  if (part === '*') return null
  const nums: number[] = []
  for (const chunk of part.split(',')) {
    const m = /^(\d+)(?:-(\d+))?$/.exec(chunk)
    if (!m) return 'bad'
    const from = Number(m[1])
    const to = m[2] === undefined ? from : Number(m[2])
    if (to < from) return 'bad'
    for (let i = from; i <= to; i += 1) nums.push(i)
  }
  return nums
}

/** 单个值：``null``（不限）/ 数字；多值或认不出返回 ``'bad'``（交给「自定义」）。 */
function singleValue(field: Field): number | null | 'bad' {
  if (field === 'bad') return 'bad'
  if (field === null) return null
  return field.length === 1 ? field[0] : 'bad'
}

export function parseCron(expr: string): CronDraft {
  const base: CronDraft = {
    mode: 'everyMinute',
    every: 5,
    minute: 0,
    hour: 9,
    weekDays: [1],
    monthDays: [1],
    raw: expr,
  }
  const custom = (): CronDraft => ({ ...base, mode: 'custom', raw: expr })

  const parts = splitFields(expr)
  // 空串（还没配过）：从「每分钟」起步 —— 这正是要避免手输的场景，别甩一个空输入框给人
  if (!parts) return expr.trim() ? custom() : { ...base, mode: 'everyMinute', raw: '' }
  const hasSeconds = parts.length === 6
  const sec = hasSeconds ? parts[0] : '*'
  const [minP, hourP, domP, monP, dowP] = hasSeconds ? parts.slice(1) : parts
  // 秒级（非 0）不擅自降成 5 段 —— 原样留给「自定义」
  if (sec !== '*' && sec !== '0') return custom()

  // 分钟段：``*/n`` 是「每 n 分钟」，单值是「第 M 分」，多值认不出
  let every = 0
  let minute: number | null = null
  const step = /^\*\/(\d+)$/.exec(minP)
  if (step) {
    every = Number(step[1])
  } else {
    const parsed = parseField(minP)
    if (parsed === 'bad') return custom()
    if (parsed !== null) {
      const one = singleValue(parsed)
      if (one === 'bad') return custom()
      minute = one
    }
  }

  const hourField = parseField(hourP)
  if (hourField === 'bad') return custom()
  const hour = singleValue(hourField)
  if (hour === 'bad') return custom()

  const dom = parseField(domP)
  const mon = parseField(monP)
  const dow = parseField(dowP)
  if (dom === 'bad' || mon === 'bad' || dow === 'bad') return custom()
  if (mon !== null) return custom() // 限定月份的场景暂不可视化，原样保留

  const m = minute ?? 0
  const h = hour ?? 0
  if (every > 0 && hour === null && dom === null && dow === null) {
    return { ...base, mode: 'everyNMinutes', every, minute: 0, hour: 0 }
  }
  if (minute === null && hour === null && dom === null && dow === null) {
    return { ...base, mode: 'everyMinute' }
  }
  if (hour === null && dom === null && dow === null) {
    return { ...base, mode: 'hourly', minute: m }
  }
  if (dom === null && dow === null) return { ...base, mode: 'daily', minute: m, hour: h }
  if (dom === null && dow) return { ...base, mode: 'weekly', minute: m, hour: h, weekDays: dow }
  if (dow === null && dom) return { ...base, mode: 'monthly', minute: m, hour: h, monthDays: dom }
  return custom()
}

export function buildCron(d: CronDraft): string {
  const minute = clamp(d.minute, 0, 59)
  const hour = clamp(d.hour, 0, 23)
  const join = (nums: number[]) => [...new Set(nums)].sort((a, b) => a - b).join(',')
  switch (d.mode) {
    case 'everyMinute':
      return '* * * * *'
    case 'everyNMinutes':
      return `*/${clamp(d.every, 1, 59)} * * * *`
    case 'hourly':
      return `${minute} * * * *`
    case 'daily':
      return `${minute} ${hour} * * *`
    case 'weekly':
      return `${minute} ${hour} * * ${join(d.weekDays) || '*'}`
    case 'monthly':
      return `${minute} ${hour} ${join(d.monthDays) || '*'} * *`
    case 'custom':
      return d.raw
  }
}

/** 一句人话：让用户确认「我点出来的是不是这个意思」 */
export function describeCron(d: CronDraft): string {
  const mm = String(clamp(d.minute, 0, 59)).padStart(2, '0')
  const hh = String(clamp(d.hour, 0, 23)).padStart(2, '0')
  switch (d.mode) {
    case 'everyMinute':
      return '每分钟跑一次'
    case 'everyNMinutes':
      return `每 ${clamp(d.every, 1, 59)} 分钟跑一次`
    case 'hourly':
      return `每小时的第 ${mm} 分跑一次`
    case 'daily':
      return `每天 ${hh}:${mm} 跑一次`
    case 'weekly': {
      const days = [...new Set(d.weekDays)].sort((a, b) => a - b)
      // 一个都没选 = 每天都跑（生成出来就是 ``*``，别写成「每每天」）
      if (days.length === 0) return `每天 ${hh}:${mm} 跑一次`
      const names = days.map((dow) => `周${WEEK_LABELS[dow] ?? dow}`).join('、')
      return `每${names} ${hh}:${mm} 跑一次`
    }
    case 'monthly': {
      const days = [...new Set(d.monthDays)].sort((a, b) => a - b)
      if (days.length === 0) return `每天 ${hh}:${mm} 跑一次`
      const names = days.map((n) => `${n} 号`).join('、')
      return `每月 ${names} ${hh}:${mm} 跑一次`
    }
    case 'custom':
      return '自定义表达式（下面手写，后端会校验）'
  }
}

/** 切模式：沿用当前时 / 分，缺的给个常用默认值（周一 / 1 号） */
function withMode(d: CronDraft, mode: CronMode): CronDraft {
  if (mode === d.mode) return d
  const next: CronDraft = { ...d, mode }
  if (mode === 'weekly' && next.weekDays.length === 0) next.weekDays = [1]
  if (mode === 'monthly' && next.monthDays.length === 0) next.monthDays = [1]
  if (mode === 'everyNMinutes' && next.every < 1) next.every = 5
  // 从「自定义」切回可视化：把当前表达式解析出来的值带上（解析不出来就用默认）
  if (d.mode === 'custom') return { ...parseCron(buildCron(next)), mode }
  return next
}

function toggle(nums: number[], n: number): number[] {
  return nums.includes(n) ? nums.filter((x) => x !== n) : [...nums, n]
}

export function CronPicker({ value, onChange, disabled = false }: CronPickerProps) {
  // 从 value 解析出来的真实形态（描述用它 —— 哪怕在「自定义」里手写，也要说出真实语义）
  const parsed = useMemo(() => parseCron(value), [value])
  /**
   * 用户最后选的模式。**只有两处用它**（其余一切仍从 ``value`` 解析，保持受控）：
   *
   * * 选了「自定义」就一直是输入框 —— 否则表达式才打了一半（段数还不够）就被识别成别的
   *   模式，输入框当场变成下拉，人就被打断了；
   * * 空值（还没配 / 被清空）沿用最后选的模式 —— 否则「自定义」刚选上就被空值规则
   *   （空串按「每分钟」起步）拉回去，输入框根本不出现。
   */
  const [picked, setPicked] = useState<CronMode | null>(null)
  const draft = useMemo(() => {
    if (picked === 'custom') return { ...parsed, mode: 'custom' as CronMode, raw: value }
    if (!value.trim() && picked) return { ...parsed, mode: picked, raw: '' }
    return parsed
  }, [value, parsed, picked])
  const emit = (patch: Partial<CronDraft>) => onChange(buildCron({ ...draft, ...patch }))
  /** 切模式：记下选择（见上面的 picked），并把新表达式交出去 */
  const pickMode = (mode: CronMode) => {
    setPicked(mode)
    onChange(buildCron(withMode(draft, mode)))
  }

  return (
    <div className={styles.picker}>
      <select
        className={styles.select}
        value={draft.mode}
        disabled={disabled}
        onChange={(e) => pickMode(e.target.value as CronMode)}
      >
        {MODE_LABELS.map(([mode, label]) => (
          <option key={mode} value={mode}>
            {label}
          </option>
        ))}
      </select>

      {draft.mode === 'everyNMinutes' && (
        <div className={styles.row}>
          <span className={styles.rowLabel}>每隔</span>
          <select
            className={styles.select}
            value={draft.every}
            disabled={disabled}
            onChange={(e) => emit({ every: Number(e.target.value) })}
          >
            {MINUTE_STEPS.map((n) => (
              <option key={n} value={n}>
                {n} 分钟
              </option>
            ))}
          </select>
        </div>
      )}

      {draft.mode === 'hourly' && (
        <div className={styles.row}>
          <span className={styles.rowLabel}>第</span>
          <select
            className={styles.select}
            value={draft.minute}
            disabled={disabled}
            onChange={(e) => emit({ minute: Number(e.target.value) })}
          >
            {MINUTES.map((n) => (
              <option key={n} value={n}>
                {String(n).padStart(2, '0')} 分
              </option>
            ))}
          </select>
        </div>
      )}

      {(draft.mode === 'daily' || draft.mode === 'weekly' || draft.mode === 'monthly') && (
        <div className={styles.row}>
          <span className={styles.rowLabel}>时间</span>
          <select
            className={styles.select}
            value={draft.hour}
            disabled={disabled}
            onChange={(e) => emit({ hour: Number(e.target.value) })}
          >
            {HOURS.map((n) => (
              <option key={n} value={n}>
                {String(n).padStart(2, '0')} 时
              </option>
            ))}
          </select>
          <select
            className={styles.select}
            value={draft.minute}
            disabled={disabled}
            onChange={(e) => emit({ minute: Number(e.target.value) })}
          >
            {MINUTES.map((n) => (
              <option key={n} value={n}>
                {String(n).padStart(2, '0')} 分
              </option>
            ))}
          </select>
        </div>
      )}

      {draft.mode === 'weekly' && (
        <div className={styles.row}>
          <span className={styles.rowLabel}>周几</span>
          <div className={styles.chips}>
            {WEEK_LABELS.map((label, dow) => (
              <button
                type="button"
                key={dow}
                className={`${styles.chip} ${draft.weekDays.includes(dow) ? styles.chipActive : ''}`}
                disabled={disabled}
                onClick={() => emit({ weekDays: toggle(draft.weekDays, dow) })}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      )}

      {draft.mode === 'monthly' && (
        <div className={styles.row}>
          <span className={styles.rowLabel}>几号</span>
          <div className={styles.chips}>
            {MONTH_DAYS.map((day) => (
              <button
                type="button"
                key={day}
                className={`${styles.chip} ${draft.monthDays.includes(day) ? styles.chipActive : ''}`}
                disabled={disabled}
                onClick={() => emit({ monthDays: toggle(draft.monthDays, day) })}
              >
                {day}
              </button>
            ))}
          </div>
        </div>
      )}

      {draft.mode === 'custom' && (
        <input
          className={styles.input}
          value={draft.raw}
          disabled={disabled}
          placeholder="*/5 * * * *"
          onChange={(e) => emit({ raw: e.target.value })}
        />
      )}

      <div className={styles.preview}>
        {/* 描述按真实解析结果说：手写出来的表达式也认得出语义（写「每 5 分钟」那种写法，
            照样显示「每 5 分钟跑一次」），只有真认不出才说「自定义」 */}
        <span className={styles.desc}>{describeCron(parsed)}</span>
        <code className={styles.expr}>{value.trim() ? value.trim() : '（空）'}</code>
      </div>
    </div>
  )
}

export default CronPicker
