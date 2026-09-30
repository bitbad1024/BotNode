"""平台层（platforms）：把「平台接入 + 桥接适配」收拢到一层。

分层约定：

* ``nacho.core``       核心层（日志、缓存、调度等基础能力）——**留在顶层**，是全局地基
* ``nacho.platforms``  平台层：本包，管「接入什么平台、怎么接」
    * ``onebot/``     OneBot 平台接入（反向 WS，框架当服务端）
    * ``kook/``       Kook 平台接入（正向 WS，框架当客户端）
    * ``bridge/``     桥接层：把各平台适配器统一到「规范化事件 + 能力协议」上

依赖方向：本包依赖 ``nacho.core``（logger）与 ``nacho.bots``（凭证存储）；
业务层（``nacho.api`` / ``nacho.workflow`` / ``nacho.db``）依赖 ``nacho.core``，
但不 import 本包 —— 平台的类型只在适配器实现里出现，协议一律结构化形状。
"""
