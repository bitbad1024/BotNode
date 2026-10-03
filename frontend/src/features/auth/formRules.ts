/**
 * 登录 / 注册表单共用的前端校验规则。
 *
 * **后端才是裁判**（字符集、长度都由 `tickneko/api/services/user/validation.py` 说了算），这里
 * 只为了输入时立刻给提示 —— 规则与后端那份对齐，改一边要顺手改另一边。
 */

/** 账号：3-32 位字母、数字、下划线、点、短横线。 */
export const accountPattern = /^[A-Za-z0-9_.-]{3,32}$/

export const PASSWORD_MIN_LENGTH = 8
export const PASSWORD_MAX_LENGTH = 128

/** 昵称：1-32 个字符（按字符数算，中文也算 1 个）。 */
export const NICKNAME_MIN_LENGTH = 1
export const NICKNAME_MAX_LENGTH = 32

/** 后端校验错误 details[].field 是「body.xxx」这种技术名，展示时翻译成中文标签。 */
export const FIELD_LABELS: Record<string, string> = {
  'body.account': '账号',
  'body.password': '密码',
  'body.nickname': '昵称',
}
