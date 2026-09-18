/// <reference types="vite/client" />

declare module '*.module.css' {
  const classes: { readonly [key: string]: string }
  export default classes
}

interface ImportMetaEnv {
  /** 后端根地址；留空 = 当前同源地址（开发走 vite 代理，部署走反代）。 */
  readonly VITE_API_BASE_URL?: string
  /** 是否在登录页显示演示账号（开发 true / 生产 false）。 */
  readonly VITE_SHOW_DEMO_ACCOUNTS?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
