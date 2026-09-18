# nacho.api 模块索引（MODULE MAP）

> 本文件是 `nacho/api/` 下**逐文件 → 作用**的速查索引。详细的「为什么」写在每个 `.py` 的模块
> docstring 里（`:mod:` 交叉引用），本文件只做「一眼定位」用。
>
> 同步规则：新增 / 重命名 / 删除文件时，记得更新这里。

---

## 0. 分层总览

```
nacho/api/
├── app.py            create_app()：装配（把下面这些装成一个 FastAPI 应用）
├── options.py        接口层选项（对应配置 [api]）
├── logging.py        日志接入点
├── common/           ★ 跨业务通用件（不认识任何具体业务）
│   ├── models.py         响应壳 ApiResponse/ErrorResponse…
│   ├── errors/           错误码 / 异常 / 处理器
│   ├── middlewares/      编号 + 访问日志
│   ├── dependencies.py   取请求编号
│   └── encoding.py       base64 编解码
├── api/              ★ 入口层：只管「对外怎么说」，认识 FastAPI
│   ├── auth/
│   │   ├── router.py         POST /auth/login、GET /auth/me
│   │   ├── dependencies.py   get_auth_service / bearer_scheme
│   │   ├── requests.py       LoginRequest（请求 schema）
│   │   └── responses.py      LoginData（响应 schema）
│   └── onebot/       OneBot 管理（服务本身在 nacho.onebot，按协议取用，不 import）
│       ├── router.py         在线列表 / 踢人 / 令牌签发与吊销
│       ├── protocols.py      OneBotLike / TokenRegistryLike（结构化协议）
│       ├── dependencies.py   get_onebot / CurrentUserDep
│       ├── requests.py       IssueTokenRequest
│       └── responses.py      ClientData / TokenData / IssuedTokenData…
└── services/        ★ 业务层：只算「业务怎么办」，不认识 FastAPI
    ├── user/        用户：models/protocols/security/store/validation
    └── auth/        鉴权：models/service/protocols/security
```

**依赖方向（单向、无环）**：

```
api/*  ──►  services/*  ──►  (services/auth ──► services/user)
  │            │
  └──────────► common/*  ◄── (所有层都能用，通用件不认识业务)
```

- 入口层 `api/` 可以依赖业务层 `services/` 与通用件 `common/`；
- 业务层 `services/` **绝不** import 入口层 `api/`；
- 想加一个业务模块：逻辑丢 `services/`、HTTP 入口丢 `api/`、通用件 `common/` 不动。

---

## 1. 顶层装配与配置

| 文件 | 作用 |
|---|---|
| `nacho/api/__init__.py` | 接口层总览 + 公开导出（`create_app`、`ApiOptions`、各业务服务、错误体系、日志接入点）。分层约定写在模块 docstring 里。 |
| `nacho/api/app.py` | 唯一装配入口 `create_app()`：建 `FastAPI` → 装访问日志中间件 → 注册异常处理器 → 挂路由并把各业务服务挂到 `app.state` 供注入。不传 `user_store`/`hasher`/`tokens` 也能跑（走默认实现）。 |
| `nacho/api/options.py` | 接口层选项 `ApiOptions`（对应配置 `[api]`）。**不读配置文件**，靠 `from_mapping` 普通映射解耦；含 `DEFAULT_PREFIX`（`/api`）、`DEFAULT_TOKEN_TTL`（3600s）。 |
| `nacho/api/logging.py` | 日志接入点：`api`（业务日志）/`api.access`（访问日志）两个 logger 名；`attach_api_logging()` 挂载文件出口。 |

---

## 2. common/ —— 横向通用件（不认识任何业务）

> 这一段是「每个业务都要用」的横切能力，因此不塞进任何业务模块。加业务不需要动这里。

| 文件 | 作用 |
|---|---|
| `common/__init__.py` | 汇总导出通用件（响应壳 / 错误 / 中间件 / 取请求编号）。 |
| `common/models.py` | 响应壳：`ApiResponse` / `ErrorResponse` / `ErrorPayload` / `ErrorDetail`，以及 `DEFAULT_TRACE_ID`。不知道 `data` 里装的是什么。 |
| `common/errors/__init__.py` | 汇总导出错误体系。 |
| `common/errors/codes.py` | 错误码与 HTTP 状态：`ErrorCode` / `HttpStatus`。 |
| `common/errors/exceptions.py` | 异常类型：`ApiError` 及其子类（`ValidationError`、`InvalidCredentialsError`、`AccountDisabledError`、`UnauthorizedError`、`TokenInvalidError`、`TokenExpiredError`、`InternalError`）。状态码挂在异常上。 |
| `common/errors/handlers.py` | `register_exception_handlers` / `to_error_details`：把异常翻成统一的 `ErrorResponse` 形状。 |
| `common/middlewares/__init__.py` | 汇总导出中间件。 |
| `common/middlewares/request_log.py` | `RequestLogMiddleware`：每次请求生成编号、写访问日志一行（方法 / 路径 / 状态码 / 耗时 / trace_id）。 |
| `common/dependencies.py` | 路由通用依赖 `trace_id_of`：取本次请求的编号。 |
| `common/encoding.py` | base64 编解码：哈希串与令牌共用。 |

---

## 3. api/ —— 入口层（认识 FastAPI）

> 只管「对外怎么说」：路由、依赖注入、请求 / 响应 schema。业务怎么算在 `services/`。

### 3.1 入口层总览

| 文件 | 作用 |
|---|---|
| `api/__init__.py` | 入口层总览，导出 `auth_router`、`onebot_router`。 |
| `api/auth/__init__.py` | 鉴权入口汇总（登录 / 当前用户两个接口）。 |
| `api/onebot/__init__.py` | OneBot 管理入口汇总（在线客户端列表 / 踢人 / 令牌签发与吊销）。 |

### 3.2 api/auth/ —— 鉴权入口

| 文件 | 作用 |
|---|---|
| `api/auth/router.py` | **HTTP 入口**：`POST <prefix>/auth/login`（账号+密码换令牌）、`GET <prefix>/auth/me`（令牌换当前用户资料）。自身不含业务判断，只翻译请求 / 装配响应。 |
| `api/auth/dependencies.py` | 路由注入件：`get_auth_service`（从 `app.state` 取服务）、`bearer_scheme`/`BearerDep`/`AuthServiceDep`。 |
| `api/auth/requests.py` | 请求体 `LoginRequest`，字段复用 `services.user.validation` 的 `Account` / `Password`。 |
| `api/auth/responses.py` | 响应体 `LoginData`（令牌 + 有效期 + 用户资料），用户资料复用 `UserProfile`。 |

### 3.3 api/onebot/ —— OneBot 管理入口

> 把 OneBot 的「在线客户端列表」与「令牌管理」做成 HTTP 接口。服务本身在 `nacho.onebot`，
> 由主程序装配时传进 `create_app(onebot=...)`；**这里不 import `nacho.onebot`**（那样等于
> 装 api 就必装 websockets），只按 `protocols.py` 里的结构化协议取用。

| 文件 | 作用 |
|---|---|
| `api/onebot/router.py` | **HTTP 入口**：`GET <prefix>/onebot/clients`（在线列表，可 `?account=` 过滤）、`DELETE <prefix>/onebot/clients/{id}`（踢下线，`?revoke=true` 连令牌一起吊销）、`GET/POST <prefix>/onebot/tokens`（列表 / 签发）、`PATCH <prefix>/onebot/tokens/{id}`（启用 / 停用，停用会断开客户端）、`DELETE <prefix>/onebot/tokens/{id}`（吊销并断开）。全部要求登录。 |
| `api/onebot/protocols.py` | 结构化协议：`OneBotLike` / `TokenRegistryLike` / `ClientLike` / `TokenLike`（数据成员写成**只读属性**，对面是冻结数据类）。靠它做到两边互不 import。 |
| `api/onebot/dependencies.py` | 路由注入件：`get_onebot`（从 `app.state` 取服务，没接入回 503）、`CurrentUserDep`（要求登录，401）。 |
| `api/onebot/requests.py` | 请求体 `IssueTokenRequest`（给哪个账号签、备注）。 |
| `api/onebot/responses.py` | 响应体：`ClientData` / `TokenData` / `IssuedTokenData`（明文令牌只在这一次出现）/ `KickData` / `RevokeData`。 |

---

## 4. services/ —— 业务层（不认识 FastAPI）

> 只算「业务怎么办」：查人、验密码、签令牌。失败抛 `common.errors.ApiError`。

### 4.1 业务层总览

| 文件 | 作用 |
|---|---|
| `services/__init__.py` | 业务层汇总导出。 |
| `services/auth/__init__.py` | 鉴权业务汇总。 |
| `services/user/__init__.py` | 用户模块汇总。 |

### 4.2 services/user/ —— 用户域（不管 HTTP）

| 文件 | 作用 |
|---|---|
| `services/user/models.py` | `UserRecord`（内部形状，**带 `password_hash`**）/ `UserProfile`（对外资料，**无密码字段**）/ `profile_of`（两者转换，显式决定露不露字段）。 |
| `services/user/validation.py` | 账号 / 密码规则：`Account` / `Password`（pydantic `AfterValidator`，`SecretStr` 包密码）。登录、将来注册 / 改资料共用。 |
| `services/user/protocols.py` | 能力协议（只声明不实现）：`UserStore`（按账号 / 按 id 查人，异步）、`PasswordHasher`（hash / verify）。 |
| `services/user/security.py` | 默认实现 `Pbkdf2PasswordHasher`：PBKDF2-SHA256，串自带算法 / 迭代 / 盐 / 摘要，定长比对。 |
| `services/user/store.py` | 默认实现 `InMemoryUserStore`：内存字典（O(1) 双索引）；`demo()` 造三个固定账号（`admin` / `robot` / `guest` 已停用）。 |
| `services/user/store_sql.py` | 落库实现 `SqlUserStore`：`UserTable`（SQLModel）声明表结构与约束，DDL 由 SQLAlchemy 按方言生成（sqlite / mariadb 同一份定义），查询走 `AsyncSession`，**不手写 SQL**；`ensure_schema` 建表、`seed_demo` 空表种演示账号。 |
| `services/user/demo.py` | 演示账号 `DEMO_USERS` 的单一来源：内存版与落库版共用，改账号只改一处。 |

### 4.3 services/auth/ —— 鉴权业务

| 文件 | 作用 |
|---|---|
| `services/auth/models.py` | 业务层 I/O（不认识 HTTP）：`Credentials`（凭据输入）、`LoginResult`（签发结果）。 |
| `services/auth/protocols.py` | 能力协议（只声明不实现）：`TokenService`（issue / parse）、`TokenClaims`（令牌里的东西，所有实现必须产出同一形状）。 |
| `services/auth/security.py` | 默认实现 `HmacTokenService`：HMAC-SHA256 不透明令牌（非 JWT）；`resolve_secret` 管缺省密钥。 |
| `services/auth/service.py` | **业务编排** `AuthService`：查人 → 比密码 → 查停用 → 签令牌；以及 `current_user`（令牌 → 查人）。依赖走 `services.user` 与本模块协议，换库 / 换算法 / 换令牌形式都不用改这里。 |

---

## 5. 速查：新东西放哪

| 要加的东西 | 放哪 |
|---|---|
| 新路由 / 新 HTTP 接口 | `api/<业务>/router.py` |
| 请求 / 响应字段定义 | `api/<业务>/requests.py`、`responses.py` |
| 路由里取服务 / 解析 token 的依赖 | `api/<业务>/dependencies.py` |
| 查库、验密、算令牌等纯逻辑 | `services/<业务>/service.py` 及同层 `*.py` |
| 业务自己的数据模型 / 协议 | `services/<业务>/models.py`、`protocols.py` |
| 跨业务共享（响应壳 / 错误 / 日志 / 编码） | `common/` |
