"""已登记类型、执行器尚未实现的内置节点（占位声明）。

这些类型在画布 / 历史图里已经在用，校验规则必须认，但执行语义还没落地。规则在注册时
一次声明（必填字段、分流出边、表达式字段），**执行器留空**：图能保存、能校验，真跑到
它们时运行器按「暂无执行器」报错——与历史行为一致。

执行器就位后，给对应类型补一个模块、用 :func:`register_node` 登记函数即可，
这里的声明会被覆盖，不用改校验器。
"""
from __future__ import annotations

from .base import ConfigField
from .registry import declare_node_type

#: 网关节点：至少两条出边才能分流（min_outgoing=2）
declare_node_type("gateway", min_outgoing=2)

#: 审批节点：必须指定审批人
declare_node_type("approval", fields=[ConfigField("assignee", "审批人", required=True)])

#: 表达式节点：表达式必填，内容交图级表达式语法检查器过一遍
declare_node_type(
    "expression",
    fields=[ConfigField("expression", "表达式", required=True)],
    expression_field="expression",
)

#: 条件节点：条件表达式必填
declare_node_type("condition", fields=[ConfigField("condition", "条件", required=True)])

#: 人工任务节点：当前没有必填字段，执行器未实现
declare_node_type("task")
