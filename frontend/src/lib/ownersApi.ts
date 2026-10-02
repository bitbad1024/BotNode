/**
 * 归属清单：与后端 `GET /owners` 一一对应。
 *
 * 管理员能看到所有人的工作流 / 机器人，列表要按归属筛（`?owner_id=`）—— 筛之前得先知道
 * 「有哪些归属」，就是这里给的；昵称一起回来，列表里显示归属不必再查人。
 *
 * **普通用户只会拿到自己那一条**（服务端按登录身份给）：所以下拉对他通常是隐藏的
 * （选项不足两个，见 `common/OwnerFilter`）。
 */
import { http } from './http'

/** 一个归属（用户）—— 筛选下拉里的一行。 */
export interface Owner {
  /** 归属标识：工作流 / 机器人上存的就是它（用户 id） */
  owner_id: string
  /** 登录账号：认人比昵称可靠（昵称可能重名、也可能没设过） */
  account: string
  /** 昵称（空串 = 没设过） */
  nickname: string
}

/** GET /owners：可选归属（管理员 = 全部用户；普通用户 = 只有自己）。 */
export function fetchOwners() {
  return http.get<Owner[]>('/owners')
}
