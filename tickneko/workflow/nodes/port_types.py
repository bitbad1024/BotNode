"""端口类型定义 —— **唯一要改的地方**。

画布能连什么类型的线、每种类型什么色、是不是数据端口，全由 :data:`PORT_TYPES` 决定。
加/改端口类型**只改这个文件**：目录接口 ``port_types`` 把它下发到前端（见
``api/workflow/responses.py``），画布配色 / 面板图例 / 接线语义自动跟着变，前端零改动。

``PortType`` 是给节点端口声明做类型检查用的 Literal（``PortSpec("text", "message", ...)``），
**必须与 :data:`PORT_TYPES` 的键保持一致** —— 加了新类型记得两边同步。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: 端口的类型字面量：给 :class:`PortSpec` 的端口声明做静态检查。
#: 和下面的 :data:`PORT_TYPES` 键一一对应，改了表记得改这里。
#:
#: 每种类型的语义：
#: trigger（控制流）决定「什么时候执行下一个节点」；
#: message（数据流）传内容；
#: target（数据流）传「发到哪」的会话定位值（:class:`~tickneko.platforms.bridge.models.ChatTarget`
#: 或平台特化 target；workflow 本身不 import bridge，值由装配层放进 ``trigger_data``）；
#: list（数据流）传**列表**容器（Python ``list``，值沿边原样投递，元素类型由产出节点负责）；
#: dict（数据流）传**字典**容器（Python ``dict``，键 / 值类型都由产出节点负责）；
#: set（数据流）传**集合**容器（Python ``set``，元素不重复，去重逻辑由产出节点负责）；
#: generic（数据流）传**透传泛型**：输入是什么类型，输出就是什么类型 —— 只许接数据流端口
#:   （接线语义见 :func:`port_types_compatible`），不能接触发。
PortType = Literal["trigger", "message", "target", "list", "dict", "set", "generic"]


@dataclass(frozen=True)
class PortTypeDef:
    """一种端口类型的展示信息：画布图例 / 端口配色 / 「是否数据端口」全从它来。

    目录接口 ``port_types`` 把它下发给前端 —— 加端口类型只改下面的 :data:`PORT_TYPES`，
    画布不用动。``data=True`` 表示沿边送值（message / target / list / dict / set /
    generic），``data=False`` 只表达先后（trigger）。泛型类型不需要额外标记字段：
    接线时谁是真泛型直接按类型名 ``generic`` 判断（见 :func:`port_types_compatible`）。
    """

    type: str
    label: str
    color: str
    data: bool = True


#: 端口类型定义表（顺序即目录接口里 ``port_types`` 的顺序）。
#: 画布配色 / 图例 / 数据流语义都跟着它走，别在前端再抄一份。
PORT_TYPES: dict[str, PortTypeDef] = {
    "trigger": PortTypeDef("trigger", "触发（控制流）", "#22c55e", data=False),
    "message": PortTypeDef("message", "消息（数据流）", "#3b82f6"),
    "target": PortTypeDef("target", "会话定位（target）", "#f59e0b"),
    "list": PortTypeDef("list", "列表（数据流）", "#a855f7"),
    "dict": PortTypeDef("dict", "字典（数据流）", "#06b6d4"),
    "set": PortTypeDef("set", "集合（数据流）", "#ec4899"),
    "generic": PortTypeDef("generic", "透传（泛型）", "#64748b"),
}


def port_types_compatible(source_type: str, target_type: str) -> bool:
    """两种端口类型能不能互接：**同类互通；泛型端口跟任何数据流端口互接**（不接触发）。

    * ``message`` -> ``message``：同类，放行；
    * ``generic`` -> ``target`` / ``target`` -> ``generic`` / ``generic`` -> ``generic``：
      泛型是「输入什么就输出什么」，数据流端口都能接 —— 但 ``generic`` -> ``trigger``
      不放行（泛型只走数据流，不接控制流）；
    * ``message`` -> ``target``：两边都非泛型且不同类，不放行（严格类型匹配仍然有效）。

    泛型就一个，判断直接按类型名 ``generic`` 来 —— 加别的端口类型不用动这里；
    另一端是不是数据流端口查 :data:`PORT_TYPES` 的 ``data``（trigger 为 False）。
    前端画布用同一份语义做接线判断（``catalog.ts`` 的 ``portCompatible``）。
    认不出的类型按「非数据、非泛型」处理 —— 宁可挡下也不放行。
    """
    if source_type == target_type:
        return True
    if source_type == "generic":
        target_def = PORT_TYPES.get(target_type)
        return bool(target_def and target_def.data)
    if target_type == "generic":
        source_def = PORT_TYPES.get(source_type)
        return bool(source_def and source_def.data)
    return False
