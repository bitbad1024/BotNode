/**
 * 个人设置接口：与后端 /api/profile/* 一一对应。
 *
 * 头像走字节流：上传时 body 就是图片字节，下载时直接拿 URL 当 <img src>。
 */
import { http } from '../../lib/http'
import type { UserProfile } from './authApi'

/** GET /profile：我的资料 + 头像信息。 */
export function fetchProfile() {
  return http.get<UserProfile>('/profile')
}

/** PATCH /profile：改昵称（1-32 个字符，后端规则与注册一致）。 */
export function updateNickname(nickname: string) {
  return http.patch<UserProfile, { nickname: string }>('/profile', { nickname })
}

/** PUT /profile/avatar：上传头像（body 就是图片字节）。 */
export function uploadAvatar(data: Blob) {
  return http.put<UserProfile, Blob>('/profile/avatar', data, {
    headers: { 'Content-Type': 'application/octet-stream' },
  })
}

/** DELETE /profile/avatar：删掉头像（没设过也算成功）。 */
export function deleteAvatar() {
  return http.del<UserProfile>('/profile/avatar')
}
