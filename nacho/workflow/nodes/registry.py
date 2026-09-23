"""节点执行器的注册表：类型名 -> 执行函数。

一张**进程级、内存里**的表（不落库、没有配置文件）：包一被 import，各节点模块就自己往
这里登记一次。所以「注册一个节点」= 让那个模块被 import 到，两种写法任选：

* 写在自己的节点模块里（推荐，跟内置节点一样）：:func:`register_node` 装饰器；
* 或者运行时手工登记：:func:`register_executor`。

别人的模块怎么被 import 进来？启动时 :func:`load_node_modules` 一行搞定，不用改框架里的
任何文件::

    from nacho.workflow import load_node_modules
    load_node_modules("my_pkg.dingtalk")     # 它自己的注册随之生效
"""
from __future__ import annotations

from collections.abc import Callable
from importlib import import_module

from .base import NodeExecutor

#: 节点类型名 -> 执行函数
_EXECUTORS: dict[str, NodeExecutor] = {}


def register_executor(node_type: str, executor: NodeExecutor) -> None:
    """注册某类型节点的执行函数；重复注册覆盖（方便测试换实现）。"""
    _EXECUTORS[node_type] = executor


def register_node(node_type: str) -> Callable[[NodeExecutor], NodeExecutor]:
    """装饰器写法：在节点函数上标一下类型就完成注册。

        @register_node("http")
        async def exec_http(node, ctx: NodeExecutionContext) -> dict[str, Any]:
            ...

    返回的是原函数（不做包装），所以装饰完照样能直接调用 / 拿去测试。
    """

    def decorate(executor: NodeExecutor) -> NodeExecutor:
        register_executor(node_type, executor)
        return executor

    return decorate


def get_executor(node_type: str) -> NodeExecutor | None:
    """取某类型节点的执行函数；没注册返回 ``None``（由运行器报「暂无执行器」）。"""
    return _EXECUTORS.get(node_type)


def registered_types() -> tuple[str, ...]:
    """已注册的类型名（排序后）。排查「我那个节点到底注册上没有」时看它。"""
    return tuple(sorted(_EXECUTORS))


def load_node_modules(*module_names: str) -> list[str]:
    """把外部的节点模块 import 进来（它们自己的注册随之生效），返回加载的模块名。

    写的节点放在自己的包里，启动时这样装进来即可::

        load_node_modules("my_pkg.dingtalk", "my_pkg.jira")

    * 同一个模块重复加载是幂等的（命中 ``sys.modules`` 缓存，模块体只会执行一次）；
    * 模块 import 失败**直接抛出去** —— 「节点没注册上」要当场看见，别等到跑图时才报
      「暂无执行器」。
    """
    for name in module_names:
        import_module(name)
    return list(module_names)
