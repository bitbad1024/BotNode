/**
 * 模块占位页：后端接口尚未开放的模块先用统一、有信息量的骨架，
 * 不是空荡的“建设中”——说明用途与计划能力，之后直接替换内容区。
 *
 * 接口地址一类实现细节不显示在界面上（界面是给人用的，不是给调用方看的）。
 */
import type { ComponentType, ReactNode } from 'react'
import styles from './PlaceholderPage.module.css'

export interface PlaceholderPageProps {
  icon: ComponentType<{ size?: number }>
  title: string
  description: string
  /** 计划能力清单 */
  features: string[]
  /** 右上角额外操作（可选） */
  actions?: ReactNode
}

export default function PlaceholderPage({
  icon: Icon,
  title,
  description,
  features,
  actions,
}: PlaceholderPageProps) {
  return (
    <div className="rise">
      <header className={styles.head}>
        <div>
          <h2 className={styles.title}>{title}</h2>
          <p className={styles.desc}>{description}</p>
        </div>
        {actions}
      </header>

      <div className={`card ${styles.panel}`}>
        <div className={styles.hero}>
          <span className={styles.heroIcon}>
            <Icon size={30} />
          </span>
          <div>
            <h3 className={styles.heroTitle}>接口接入中</h3>
            <p className={styles.heroDesc}>
              该模块界面骨架已就绪，待后端开放对应 HTTP 接口后即可填充真实数据。
            </p>
          </div>
        </div>

        <div className={styles.body}>
          <div className={styles.block}>
            <div className={styles.blockLabel}>计划能力</div>
            <ul className={styles.featureList}>
              {features.map((f) => (
                <li key={f} className={styles.featureItem}>
                  <span className={styles.bullet} />
                  {f}
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </div>
  )
}
