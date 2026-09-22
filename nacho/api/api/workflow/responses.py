"""工作流接口的响应体：校验报告 / 工作流 / 版本（套进统一响应壳 ``data`` 里）。"""
from __future__ import annotations

from typing import Any

from pydantic import Field

from ...common.models import _Frozen
from nacho.workflow import (
    ValidationIssue,
    ValidationReport,
    WorkflowDefinitionRecord,
    WorkflowVersionRecord,
)


class ValidationIssueData(_Frozen):
    """一条校验错误（字段名与前端约定：nodeId 驼峰）。"""

    nodeId: str = ""
    code: str
    message: str
    suggestion: str = ""

    @classmethod
    def from_issue(cls, issue: ValidationIssue) -> ValidationIssueData:
        return cls(
            nodeId=issue.node_id,
            code=issue.code,
            message=issue.message,
            suggestion=issue.suggestion,
        )


class ValidationReportData(_Frozen):
    """校验结论：``valid=false`` 时带阶段与错误明细（HTTP 仍为 200——这是业务结果不是请求错误）。"""

    valid: bool
    stage: str | None = None
    errors: list[ValidationIssueData] = Field(default_factory=list)

    @classmethod
    def from_report(cls, report: ValidationReport) -> ValidationReportData:
        return cls(
            valid=report.valid,
            stage=report.stage,
            errors=[ValidationIssueData.from_issue(issue) for issue in report.errors],
        )


class WorkflowData(_Frozen):
    """工作流定义（列表 / 详情 / 改名后都返回它）。"""

    id: str
    owner_id: str
    name: str
    status: str
    current_version: int
    published_version: int
    created_at: float
    updated_at: float

    @classmethod
    def from_record(cls, record: WorkflowDefinitionRecord) -> WorkflowData:
        return cls(
            id=record.id,
            owner_id=record.owner_id,
            name=record.name,
            status=record.status,
            current_version=record.current_version,
            published_version=record.published_version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class WorkflowVersionData(_Frozen):
    """一个版本快照：图以解析后的对象返回（不再让前端 parse 字符串）。"""

    id: str
    workflow_id: str
    owner_id: str
    version: int
    graph: dict[str, Any]
    checksum: str
    note: str
    created_at: float

    @classmethod
    def from_record(cls, record: WorkflowVersionRecord) -> WorkflowVersionData:
        return cls(
            id=record.id,
            workflow_id=record.workflow_id,
            owner_id=record.owner_id,
            version=record.version,
            graph=record.graph().model_dump(mode="json"),
            checksum=record.checksum,
            note=record.note,
            created_at=record.created_at,
        )


class SaveVersionResultData(_Frozen):
    """保存版本的结果：工作流最新状态 + 命中的版本 + 这次有没有真的产生新版本。"""

    workflow: WorkflowData
    version: WorkflowVersionData
    created: bool = Field(description="图内容与最新版本一致时为 false（去重，不新增版本）")
