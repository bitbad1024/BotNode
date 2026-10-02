# 缓存层（`nacho.core.cache`）

## 设计要点

- **一套 API，两种后端**：`backend = "redis"` 走 Redis（redis-py 异步客户端），
  `"memory"`（默认）走进程内存；两者实现同一个协议（`CacheBackend`），键是字符串、
  值分字符串 / 列表 / 哈希三种结构、TTL 按秒，语义一致 —— 换后端不用改上层代码；
- **业务代码只碰进程级单例** `cache`（`from nacho.core.cache import cache`）；要一份
  独立的缓存就自己 `Cache()`（比如测试里）；
- **不隐式启动**：没 `start()` 就调数据接口会抛 `CacheError` —— 连不连得上应该在启动
  阶段就见分晓，而不是等到哪一次 `get` 才炸；
- **连不上不静默**：配了 Redis 但连不上，默认**当场抛错**并在报错信息里提示检查
  `config.toml` 的 `[cache.redis]`；只有显式打开 `fallback_to_memory` 才退回本地缓存并
  记一条 warning（要上报就问 `Cache.degraded`）；
- **命名空间**：Redis 上所有键都带 `<namespace>:` 前缀（多个应用共用一个实例时隔离），
  上层看到 / 传进来的键名始终是不带前缀的那一份；
- **配置由上层传**：`[cache]` 区域（见 `config.py` / `config.toml.example`）的映射由
  `CacheOptions.from_mapping` 转成选项，本层不 import `config` —— 和日志核心、调度器
  一样，配置由 `app.py` 传下来。

## 职责划分

| 模块 | 职责 |
| --- | --- |
| `core.Cache` | 缓存门面：抹平后端差异，定下「`ttl=None` 用哪个 TTL」 |
| `interfaces.CacheBackend` | 后端协议，两个实现都符合它 |
| `memory.MemoryCache` | 进程内存实现，也是 Redis 连不上时的降级兜底 |
| `redis.RedisCache` | Redis 实现（可选依赖 `pip install "nacho[redis]"`） |
| `models` | `CacheOptions` / `RedisOptions` / `CacheError` |
| `manager.cache` | 进程级单例 |
| `logging` | 本层的日志接入点 |

## 快速开始

```python
from nacho.core.cache import CacheOptions, cache

await cache.start()                     # 默认就是本地内存版：不启用 Redis 也能用
await cache.set("k", "v", ttl=60)
await cache.get("k")

await cache.list_push("queue", "a", "b")            # 列表
await cache.hash_set("user:1", {"name": "阿一"})    # 哈希
await cache.set_json("profile", {"tags": ["a"]})    # 嵌套结构走 JSON

cache.configure(CacheOptions(backend="redis", namespace="nacho"))   # 要用 Redis
await cache.start()
```

`start()` / `stop()` 都是显式的、重复调用是空操作。

## 后端协议约定

两个后端必须一致，差异在协议层被抹平：

- **键**是普通字符串、**不带前缀**（前缀是 Redis 后端自己的事）；
- **值**有三种结构：字符串（`get` / `set`）、列表（`list_*`）、哈希（`hash_*`）。
  元素与字段值都是 `str` —— Redis 后端开 `decode_responses` 直接拿回字符串，上层不必
  解码字节；收到别的类型一律抛 `CacheError`，不会被悄悄塞进去。要存嵌套结构（哈希的
  哈希、对象、数组）就用 `Cache.set_json` 装成一段 JSON 文本；
- **结构不能混用**：一个键按哪种结构写入，就只能按那种结构访问，换一种访问抛
  `CacheError`（对应 Redis 的 `WRONGTYPE`）—— 唯一的例外是 `get_many`，它遇到非字符串
  键当作没取到（照 `MGET` 来）；
- **空结构不占键**：列表被弹空、哈希字段被删空之后，这个键就不存在了（与 Redis 一致）；
- **TTL** 是秒（`float`）：`set(..., ttl=None)` 表示永不过期，`ttl()` 用 `None` 表示键不
  存在、`math.inf` 表示永不过期；`list_*` / `hash_*` 上的 `ttl` **只在新建键时生效**，
  往已有键上追加不动它的过期时间（照 Redis 来：`RPUSH` / `HSET` 不刷新 TTL）；
- **批量**方法要么全做要么不做（Redis 后端走 pipeline / 单条多键命令），不返回半截结果；
- **生命周期**：`start` / `stop`，重复调用是空操作；出错一律抛 `CacheError`
  （驱动层异常不外泄）。

## 两个后端各自的注意点

**内存后端**：数据只活在当前进程里 —— 换进程就没了，也不跨机器共享，换来的是零依赖、
零网络；每条记录带类型标签，拿另一种结构访问同一个键抛 `CacheError`；过期键由后台任务
按 `sweep_interval`（默认 30 秒）扫掉。

**Redis 后端**：一条命令一个协程，不阻塞事件循环，连接池由驱动自己管；所有键拼上
`<namespace>:` 前缀，`keys()` 用 `SCAN` 游标遍历（`KEYS` 在大库上会阻塞整个 Redis），
返回前把前缀去掉；驱动的 `RedisError` 一律翻成 `CacheError`，上层不必 import redis 就能
接住缓存层的错。没装 redis 包时 `start()` 报 `CacheError` 并给出安装提示。

## 选项与配置

两份不可变 dataclass：`CacheOptions`（`backend` / `namespace` / `default_ttl` /
`fallback_to_memory` / `sweep_interval` / `redis`）与 `RedisOptions`（地址、账号、
超时、重试、连接池上限）。配置系统产出的是普通映射，由 `from_mapping` 转进来 ——
缺的项用默认值，多出来的键忽略（校验归配置系统管，报错也要报在配置那一层）。

## 日志接入

本层接的是 `nacho.core.logger` 的进程门面，名为 `cache`（相对核心 `nacho` ->
`nacho.cache`）。**业务模块一律不直接 `default_core()`**：要日志实例就调
`cache_logger()`；装配层也可把实例经 `Cache(..., logger=)` 传入。核心由装配层
（`nacho.bootstrap` 或 `nacho.wiring.wire_loggers`）经 `set_core()` 存进本模块槽位；
`import` 本模块**零副作用**，没装配就调 `cache_logger()` 会当场抛错（fail fast），
不会默默按默认参数建一份把配置定死的核心。
