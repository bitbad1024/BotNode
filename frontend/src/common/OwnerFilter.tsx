/**
 * 归属筛选：管理员按「谁的」看列表时用（工作流列表与机器人列表共用）。
 *
 * 选项来自 `GET /owners`：管理员拿到全部用户，普通用户只有自己那一条 —— 所以这里
 * **选项不足两个就整个不渲染**，调用方不必去判断角色（服务端已按登录身份给过）。
 */
import type { Owner } from '../lib/ownersApi'
import styles from './OwnerFilter.module.css'

/**
 * 归属的显示名：昵称优先，其次登录账号，最后回落到 id。
 *
 * 列表里显示归属用它 —— 后端只保证 `owner_id` 一定有值，昵称可能是空串（没设过 / 查不到）。
 */
export function ownerName(ownerId: string, owners: Owner[]): string {
  const owner = owners.find((item) => item.owner_id === ownerId)
  if (!owner) return ownerId
  return owner.nickname || owner.account || ownerId
}

interface OwnerFilterProps {
  owners: Owner[]
  /** 选中的归属 id；空串 = 全部 */
  value: string
  onChange: (ownerId: string) => void
}

export default function OwnerFilter({ owners, value, onChange }: OwnerFilterProps) {
  if (owners.length < 2) return null
  return (
    <label className={styles.wrap}>
      <span className={styles.label}>归属</span>
      <select
        className={styles.select}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">全部</option>
        {owners.map((owner) => (
          <option key={owner.owner_id} value={owner.owner_id}>
            {owner.nickname || owner.account || owner.owner_id}
          </option>
        ))}
      </select>
    </label>
  )
}
