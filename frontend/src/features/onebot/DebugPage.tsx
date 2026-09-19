/**
 * WS 调试页（纯展示）：连接 / 令牌 / 收发记录都在 debugStore（挂在主框架上），
 * 切页面回来一切还在，连接也不会断。
 */
import { useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import { WS_PRESETS } from './presets'
import { useDebug } from './debugStore'
import type { ConnStatus, Frame } from './debugStore'
import { IconTrash } from '../../common/icons'
import styles from './DebugPage.module.css'

const STATUS_TEXT: Record<ConnStatus, string> = {
  idle: '未连接',
  connecting: '连接中…',
  open: '已连接',
  closed: '已断开',
  error: '连接失败',
}

const STATUS_CLASS: Record<ConnStatus, string> = {
  idle: styles.dotIdle,
  connecting: styles.dotConnecting,
  open: styles.dotOpen,
  closed: styles.dotClosed,
  error: styles.dotError,
}

const DIR_TEXT = { out: '↑ 发送', in: '↓ 接收', sys: '◦ 系统' } as const
const DIR_CLASS = {
  out: styles.dirOut,
  in: styles.dirIn,
  sys: styles.dirSys,
} as const

function hhmmss(at: number): string {
  const d = new Date(at)
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, '0'))
    .join(':')
}

export default function DebugPage() {
  const {
    url,
    accessToken,
    status,
    draft,
    presetId,
    autoReply,
    sendError,
    frames,
    setUrl,
    setAccessToken,
    setAutoReply,
    editDraft,
    pickPreset,
    connect,
    disconnect,
    send,
    replyOk,
    clearFrames,
  } = useDebug()

  const listRef = useRef<HTMLDivElement | null>(null)

  // 新帧自动滚到底
  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [frames])

  const preset = WS_PRESETS.find((p) => p.id === presetId)
  const open = status === 'open'

  return (
    <div className={`rise ${styles.page}`}>
      <header className={styles.head}>
        <h2 className={styles.title}>WS 调试</h2>
        <p className={styles.sub}>
          模拟 OneBot 实现连框架的反向 WS：左边选报文模板发事件，右边看框架下发的动作并回应。
          连接和记录在切页面后保留；令牌在
          <Link to="/tokens" className={styles.link}>
            令牌管理
          </Link>
          签发（明文只显示一次）。
        </p>
      </header>

      {/* 连接条 */}
      <section className={`card ${styles.conn}`}>
        <label className={styles.fieldUrl}>
          <span className={styles.fieldLabel}>WS 地址</span>
          <input
            className={styles.input}
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            spellCheck={false}
          />
        </label>
        <label className={styles.fieldToken}>
          <span className={styles.fieldLabel}>access_token</span>
          <input
            className={styles.input}
            value={accessToken}
            onChange={(e) => setAccessToken(e.target.value)}
            placeholder="必填：服务端配了令牌注册表"
            spellCheck={false}
          />
        </label>
        <div className={styles.connActions}>
          {open ? (
            <button type="button" className="btn" onClick={disconnect}>
              断开
            </button>
          ) : (
            <button
              type="button"
              className={`btn ${styles.primaryBtn}`}
              onClick={connect}
              disabled={status === 'connecting'}
            >
              连接
            </button>
          )}
          <span className={styles.status}>
            <i className={`${styles.dot} ${STATUS_CLASS[status]}`} />
            {STATUS_TEXT[status]}
          </span>
        </div>
      </section>

      <section className={styles.grid}>
        {/* 发送区 */}
        <div className={`card ${styles.pane}`}>
          <div className={styles.paneHead}>
            <h3 className={styles.paneTitle}>发送区</h3>
            <span className={styles.paneNote}>客户端 → 框架（事件 / 回应）</span>
          </div>
          <label className={styles.field}>
            <span className={styles.fieldLabel}>消息模板</span>
            <select
              className={styles.input}
              value={presetId}
              onChange={(e) => pickPreset(e.target.value)}
            >
              {WS_PRESETS.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label}
                </option>
              ))}
              <option value="custom">自定义…</option>
            </select>
          </label>
          {preset && <p className={styles.hint}>{preset.hint}</p>}
          <textarea
            className={styles.textarea}
            value={draft}
            onChange={(e) => editDraft(e.target.value)}
            rows={12}
            spellCheck={false}
          />
          {sendError && <p className={styles.sendError}>{sendError}</p>}
          <div className={styles.sendRow}>
            <button
              type="button"
              className={`btn ${styles.primaryBtn}`}
              onClick={send}
              disabled={!open}
            >
              发送
            </button>
            <span className={styles.sendHint}>{open ? '先做 JSON 校验再发' : '连接后才能发送'}</span>
          </div>
        </div>

        {/* 接收区 */}
        <div className={`card ${styles.pane}`}>
          <div className={styles.paneHead}>
            <h3 className={styles.paneTitle}>接收区</h3>
            <div className={styles.recvTools}>
              <label className={styles.check}>
                <input
                  type="checkbox"
                  checked={autoReply}
                  onChange={(e) => setAutoReply(e.target.checked)}
                />
                自动回应动作
              </label>
              <span className={styles.frameCount}>{frames.length} 条</span>
              <button
                type="button"
                className="icon-btn"
                onClick={clearFrames}
                title="清空接收区"
              >
                <IconTrash size={16} />
              </button>
            </div>
          </div>
          <div className={styles.frames} ref={listRef}>
            {frames.length === 0 && (
              <p className={styles.empty}>
                还没有报文。连接后发一条「私聊消息」—— 框架会回一个 send_msg 动作。
              </p>
            )}
            {frames.map((f: Frame) => (
              <div
                key={f.id}
                className={`${styles.frame} ${
                  f.dir === 'in'
                    ? styles.frameIn
                    : f.dir === 'out'
                      ? styles.frameOut
                      : styles.frameSys
                }`}
              >
                <div className={styles.frameMeta}>
                  <span className={`${styles.dirTag} ${DIR_CLASS[f.dir]}`}>{DIR_TEXT[f.dir]}</span>
                  <span className={styles.kindTag}>{f.kind}</span>
                  <span className={styles.frameTime}>{hhmmss(f.at)}</span>
                  {f.dir === 'in' && f.kind === '动作' && (
                    <button
                      type="button"
                      className={styles.replyBtn}
                      onClick={() => replyOk(f)}
                    >
                      回应 ok
                    </button>
                  )}
                </div>
                <pre className={styles.frameBody}>{f.text}</pre>
              </div>
            ))}
          </div>
        </div>
      </section>
    </div>
  )
}
