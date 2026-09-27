/**
 * 个人设置页：改昵称 + 换头像。
 *
 * 布局：左侧一张大卡（头像 + 表单），右侧预览卡。
 * 头像上传走 PUT /profile/avatar（body 就是图片字节），改昵称走 PATCH /profile。
 */
import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react'
import { ApiRequestError, http } from '../../lib/http'
import { ConfirmDialog } from '../../common/ConfirmDialog'
import { useAuth } from './authStore'
import { useToast } from '../../common/Toast'
import AvatarImage from './AvatarImage'
import {
  fetchProfile,
  updateNickname,
  uploadAvatar,
  deleteAvatar,
} from './profileApi'
import {
  IconAlert,
  IconCamera,
  IconCheck,
  IconTrash,
  IconUser,
} from '../../common/icons'
import { FIELD_LABELS, NICKNAME_MAX_LENGTH, NICKNAME_MIN_LENGTH } from './formRules'
import styles from './ProfilePage.module.css'

/** 头像最大体积（2 MiB，与后端默认对齐）。 */
const AVATAR_MAX_BYTES = 2 * 1024 * 1024

/** 头像合法类型（后端按文件头认，这里只作前端快速拦截）。 */
const AVATAR_TYPES: Record<string, string> = {
  'image/png': 'PNG',
  'image/jpeg': 'JPEG',
  'image/webp': 'WebP',
  'image/gif': 'GIF',
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}

export default function ProfilePage() {
  const { state, dispatch } = useAuth()
  const { pushToast } = useToast()

  const [profile, setProfile] = useState(state.user)
  const [nickname, setNickname] = useState(state.user?.nickname ?? '')
  const [savingNickname, setSavingNickname] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [error, setError] = useState<{ title: string; detail: string; traceId: string } | null>(null)
  /** 待确认的动作：改昵称 / 删头像（都要动数据，先问一声） */
  const [confirm, setConfirm] = useState<'nickname' | 'avatar' | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  /** 从接口拉最新资料（头像信息可能变了）。 */
  useEffect(() => {
    let cancelled = false
    fetchProfile()
      .then(({ data }) => {
        if (cancelled) return
        setProfile(data)
        setNickname(data.nickname)
        dispatch({ type: 'SET_USER', user: data })
        // 头像 blob 也刷新一下（可能刚换过头像）
        if (data.has_avatar && data.avatar_url) {
          http
            .getBlob(data.avatar_url)
            .then((blob) => {
              if (!cancelled) {
                dispatch({ type: 'SET_AVATAR_BLOB', url: URL.createObjectURL(blob) })
              }
            })
            .catch(() => { /* 拉不到就保留现有 */ })
        } else {
          dispatch({ type: 'SET_AVATAR_BLOB', url: undefined })
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiRequestError) {
          setError({ title: err.message, detail: '', traceId: err.traceId })
        }
      })
    return () => { cancelled = true }
  }, [dispatch])

  const hasAvatar = Boolean(profile?.has_avatar && profile?.avatar_url)
  const initial = (profile?.nickname || profile?.account || '?').slice(0, 1)

  /** 昵称是否可提交。 */
  const nicknameTrimmed = nickname.trim()
  const nicknameInvalid =
    nicknameTrimmed.length < NICKNAME_MIN_LENGTH ||
    nicknameTrimmed.length > NICKNAME_MAX_LENGTH
  const canSubmitNickname =
    !savingNickname && !nicknameInvalid && nicknameTrimmed !== (profile?.nickname ?? '')

  /** 表单提交：先弹确认，确认后才真的打接口（saveNickname）。 */
  function onSubmitNickname(e: FormEvent) {
    e.preventDefault()
    if (!canSubmitNickname) return
    setConfirm('nickname')
  }

  /** 真的改昵称。 */
  async function saveNickname() {
    if (!canSubmitNickname) return
    setSavingNickname(true)
    setError(null)
    try {
      const { data } = await updateNickname(nicknameTrimmed)
      setProfile(data)
      dispatch({ type: 'SET_USER', user: data })
      pushToast('success', '昵称已更新')
    } catch (err) {
      if (err instanceof ApiRequestError) {
        const d = err.details[0]
        const detail = d
          ? `${FIELD_LABELS[d.field] ?? d.field}：${d.message}`
          : err.message
        setError({ title: err.message, detail, traceId: err.traceId })
      } else {
        setError({ title: '修改失败', detail: '未知错误，请稍后再试', traceId: '-' })
      }
    } finally {
      setSavingNickname(false)
      setConfirm(null)
    }
  }

  /** 选择文件 -> 校验 -> 上传。 */
  async function onFileChange(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return

    if (file.size > AVATAR_MAX_BYTES) {
      setError({
        title: '文件太大',
        detail: `头像不能超过 ${formatBytes(AVATAR_MAX_BYTES)}，当前 ${formatBytes(file.size)}`,
        traceId: '-',
      })
      e.target.value = ''
      return
    }
    if (!AVATAR_TYPES[file.type]) {
      setError({
        title: '格式不支持',
        detail: '只支持 PNG / JPEG / WebP / GIF',
        traceId: '-',
      })
      e.target.value = ''
      return
    }

    setUploading(true)
    setError(null)
    try {
      const { data } = await uploadAvatar(file)
      setProfile(data)
      dispatch({ type: 'SET_USER', user: data })
      // 换头像后重新拉 blob，更新全局
      if (data.has_avatar && data.avatar_url) {
        http
          .getBlob(data.avatar_url)
          .then((blob) => dispatch({ type: 'SET_AVATAR_BLOB', url: URL.createObjectURL(blob) }))
          .catch(() => { /* 拉不到就保留现有 */ })
      }
      pushToast('success', '头像已更新')
    } catch (err) {
      if (err instanceof ApiRequestError) {
        setError({ title: err.message, detail: '', traceId: err.traceId })
      } else {
        setError({ title: '上传失败', detail: '未知错误，请稍后再试', traceId: '-' })
      }
    } finally {
      setUploading(false)
      e.target.value = ''
    }
  }

  /** 删掉头像。 */
  async function onRemoveAvatar() {
    setRemoving(true)
    setError(null)
    try {
      const { data } = await deleteAvatar()
      setProfile(data)
      dispatch({ type: 'SET_USER', user: data })
      dispatch({ type: 'SET_AVATAR_BLOB', url: undefined })
      pushToast('success', '头像已删除')
    } catch (err) {
      if (err instanceof ApiRequestError) {
        setError({ title: err.message, detail: '', traceId: err.traceId })
      } else {
        setError({ title: '删除失败', detail: '未知错误，请稍后再试', traceId: '-' })
      }
    } finally {
      setRemoving(false)
      setConfirm(null)
    }
  }

  if (!profile) {
    return (
      <div className={styles.page}>
        <div className="state-box">加载中…</div>
      </div>
    )
  }

  return (
    <div className={styles.page}>
      <div className={styles.head}>
        <h1 className={styles.title}>个人设置</h1>
        <p className={styles.sub}>修改你的昵称和头像，其他用户可以看到。</p>
      </div>

      {error && (
        <div className={styles.errorBar} role="alert">
          <span className={styles.errorIcon}>
            <IconAlert size={18} />
          </span>
          <div>
            <div className={styles.errorTitle}>{error.title}</div>
            {error.detail && <div className={styles.errorDetail}>{error.detail}</div>}
            {error.traceId !== '-' && (
              <div className={styles.errorTrace}>trace · {error.traceId}</div>
            )}
          </div>
        </div>
      )}

      <div className={styles.grid}>
        {/* 左侧：编辑卡片 */}
        <div className={styles.card}>
          <h2 className={styles.cardTitle}>头像</h2>

          <div className={styles.avatarSection}>
            <div className={styles.avatarWrap}>
              <AvatarImage initial={initial} size="xl" />
              <button
                className={styles.avatarEditBtn}
                type="button"
                title="更换头像"
                disabled={uploading}
                onClick={() => fileInputRef.current?.click()}
              >
                <IconCamera size={16} />
              </button>
            </div>

            <div className={styles.avatarInfo}>
              <p className={styles.avatarHint}>
                支持 PNG / JPEG / WebP / GIF，最大 {formatBytes(AVATAR_MAX_BYTES)}
              </p>
              {hasAvatar && profile?.avatar_size != null && (
                <p className={styles.avatarMeta}>
                  当前头像：{formatBytes(profile.avatar_size)}
                  {profile.avatar_updated_at != null && profile.avatar_updated_at > 0 && (
                    <>，更新于 {new Date(profile.avatar_updated_at * 1000).toLocaleDateString('zh-CN')}</>
                  )}
                </p>
              )}
              <div className={styles.avatarActions}>
                <button
                  className="btn"
                  type="button"
                  disabled={uploading}
                  onClick={() => fileInputRef.current?.click()}
                >
                  {uploading ? (
                    <span className={styles.busyInner}>
                      <span className="spinner" />
                      上传中…
                    </span>
                  ) : (
                    <>
                      <IconCamera size={15} />
                      {hasAvatar ? '更换头像' : '上传头像'}
                    </>
                  )}
                </button>
                {hasAvatar && (
                  <button
                    className="btn btn-danger"
                    type="button"
                    disabled={removing}
                    onClick={() => setConfirm('avatar')}
                  >
                    {removing ? (
                      <span className={styles.busyInner}>
                        <span className="spinner" />
                        删除中…
                      </span>
                    ) : (
                      <>
                        <IconTrash size={15} />
                        删除头像
                      </>
                    )}
                  </button>
                )}
              </div>
            </div>
          </div>

          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            style={{ display: 'none' }}
            onChange={onFileChange}
          />

          <div className={styles.divider} />

          <h2 className={styles.cardTitle}>昵称</h2>
          <form className={styles.form} onSubmit={onSubmitNickname} noValidate>
            <div className={styles.field}>
              <label className={styles.label} htmlFor="profile-nickname">
                展示名称
              </label>
              <div className={styles.control}>
                <span className={styles.controlIcon}>
                  <IconUser size={18} />
                </span>
                <input
                  id="profile-nickname"
                  type="text"
                  placeholder={`${NICKNAME_MIN_LENGTH}-${NICKNAME_MAX_LENGTH} 个字符`}
                  value={nickname}
                  onChange={(e) => setNickname(e.target.value)}
                  maxLength={NICKNAME_MAX_LENGTH + 10}
                />
              </div>
              {nicknameInvalid && nicknameTrimmed.length > 0 && (
                <p className={styles.fieldHint}>
                  昵称需要 {NICKNAME_MIN_LENGTH}-{NICKNAME_MAX_LENGTH} 个字符
                </p>
              )}
            </div>
            <button
              className="btn btn-primary"
              type="submit"
              disabled={!canSubmitNickname}
            >
              {savingNickname ? (
                <span className={styles.busyInner}>
                  <span className="spinner" />
                  保存中…
                </span>
              ) : (
                <>
                  <IconCheck size={15} />
                  保存昵称
                </>
              )}
            </button>
          </form>
        </div>

        {/* 右侧：预览卡片 */}
        <div className={styles.card}>
          <h2 className={styles.cardTitle}>预览</h2>
          <div className={styles.preview}>
            <div className={styles.previewAvatarWrap}>
              <AvatarImage initial={initial} size="lg" />
            </div>
            <div className={styles.previewInfo}>
              <span className={styles.previewName}>{nicknameTrimmed || '未命名'}</span>
              <span className={styles.previewAccount}>@{profile.account}</span>
              <div className={styles.previewRoles}>
                {profile.roles.map((r) => (
                  <span key={r} className={`chip ${r === 'admin' ? 'chip-accent' : ''}`}>
                    {r}
                  </span>
                ))}
              </div>
            </div>
          </div>
          <p className={styles.previewNote}>
            其他用户看到的昵称与头像如左图所示。
          </p>
        </div>
      </div>

      {confirm === 'nickname' && (
        <ConfirmDialog
          title="保存昵称？"
          body={
            <>
              展示名称会改成 <b>{nicknameTrimmed}</b>，其他用户立刻能看到（原名
              「{profile.nickname || '—'}」）。
            </>
          }
          confirmText="保存"
          danger={false}
          busy={savingNickname}
          onCancel={() => setConfirm(null)}
          onConfirm={() => void saveNickname()}
        />
      )}

      {confirm === 'avatar' && (
        <ConfirmDialog
          title="删除头像？"
          body={<>头像会被移除，之后显示昵称首字母。此操作不可撤销，但可以重新上传。</>}
          confirmText="删除头像"
          busy={removing}
          onCancel={() => setConfirm(null)}
          onConfirm={() => void onRemoveAvatar()}
        />
      )}
    </div>
  )
}
