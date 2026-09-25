"""工作流管理的 HTTP 入口：定义增删查改 + 暂存 / 版本保存 / 历史 / 发布 + 入库前校验。

    POST   <prefix>/workflows/validate             只校验不入库（画布里随时试）
    GET    <prefix>/workflows/node-types           节点类型目录（画布渲染面板 / 端口 / 表单）
    POST   <prefix>/workflows                      新建空工作流（只要名字）
    GET    <prefix>/workflows                      列表（普通用户只看自己；?owner_id= 管理员）
    GET    <prefix>/workflows/{id}                 定义详情
    PATCH  <prefix>/workflows/{id}                 改名
    DELETE <prefix>/workflows/{id}                 删除（连同全部版本）
    PUT    <prefix>/workflows/{id}/draft           暂存编辑中的图（不校验，指针切到 draft）
    GET    <prefix>/workflows/{id}/draft           读暂存区（没暂存过 graph=null）
    POST   <prefix>/workflows/{id}/versions        校验通过后提交一个版本（不过不写库）
    GET    <prefix>/workflows/{id}/versions        版本历史
    GET    <prefix>/workflows/{id}/versions/{ver}  某个版本快照
    POST   <prefix>/workflows/{id}/publish         发布版本（不传 version = 发布最新版）

全部需要登录。多用户隔离：归属 ``owner_id`` 就是创建者的用户 id；普通用户只在自己
名下操作，管理员不限，具体把关见 :mod:`.dependencies`。

校验失败（图不合法）与请求格式错误不同：前者是**业务结果**，HTTP 200 +
``{valid:false, stage, errors}``，前端按节点画红点；后者（字段缺 / 类型错）走全局
422。提交版本接口在 ``valid=false`` 时**不写任何数据**。

发布接口**只挪发布指针**，不立即执行图；已发布工作流的定时触发在服务启动时统一登记到
调度器（**只登记、不执行**，到点才跑，见 :func:`nacho.workflow.runtime.load_published_workflows`），
改了定时配置需重启生效。
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ...common.dependencies import trace_id_of
from ...common.errors import ApiError, ErrorCode, HttpStatus
from ...common.models import ApiResponse
from ...logging import api_logger
from ...services.auth.models import CurrentUser
from nacho.workflow import (
    WorkflowNameConflict,
    apply_config_defaults,
    canonical_draft_json,
    canonical_graph_json,
    graph_checksum,
    validate_graph,
)
from .dependencies import (
    CurrentUserDep,
    WorkflowStoreDep,
    get_in_scope,
    owner_filter_of,
)
from .requests import (
    CreateWorkflowRequest,
    PublishRequest,
    RenameWorkflowRequest,
    SaveDraftRequest,
    SaveVersionRequest,
    ValidateRequest,
)
from .responses import (
    NodeCatalogData,
    SaveVersionResultData,
    ValidationReportData,
    WorkflowData,
    WorkflowDraftData,
    WorkflowVersionData,
)

router = APIRouter(prefix="/workflows", tags=["工作流"])


def _audit(message: str, *, owner_id: str, **extra: object) -> None:
    """记一条工作流审计事件（``api`` 那路，落库那份就是审计时间线）。"""
    api_logger().info(message, owner_id=owner_id, **extra)


def _conflict(exc: WorkflowNameConflict) -> ApiError:
    """同名冲突统一成 409。"""
    return ApiError(
        ErrorCode.HTTP_ERROR,
        str(exc),
        status_code=status.HTTP_409_CONFLICT,
    )


# --------------------------------------------------------------------------- 校验
@router.post("/validate")
async def validate_only(
    payload: ValidateRequest,
    user: CurrentUserDep,
) -> ApiResponse[ValidationReportData]:
    """只跑入库前校验流水线，不碰数据库。"""
    _ = user  # 登录即可；校验是无状态的
    report = validate_graph(payload.graph)
    return ApiResponse(data=ValidationReportData.from_report(report))


# --------------------------------------------------------------------------- 节点目录
@router.get("/node-types")
async def list_node_types(user: CurrentUserDep) -> ApiResponse[NodeCatalogData]:
    """节点类型目录：画布照它渲染节点面板 / 端口 / 配置表单。

    只读内存里那张注册表，不碰数据库 —— 加一个节点类型只改后端（写节点那个文件），画布与
    这个接口都不用动。必须声明在 ``/{workflow_id}`` **之前**，否则会被它当成 id 抢走。
    """
    _ = user  # 登录即可；目录是无状态的
    return ApiResponse(data=NodeCatalogData.from_registry())


# --------------------------------------------------------------------------- 定义
@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workflow(
    payload: CreateWorkflowRequest,
    request: Request,
    store: WorkflowStoreDep,
    user: CurrentUserDep,
) -> ApiResponse[WorkflowData]:
    """新建空工作流（归属 = 当前登录用户）；同归属同名 409。"""
    try:
        record = await store.create(user.user.id, payload.name)
    except WorkflowNameConflict as exc:
        raise _conflict(exc) from exc
    _audit(
        "工作流已创建",
        owner_id=record.owner_id,
        workflow_id=record.id,
        name=record.name,
        trace_id=trace_id_of(request),
    )
    return ApiResponse(data=WorkflowData.from_record(record), trace_id=trace_id_of(request))


@router.get("")
async def list_workflows(
    store: WorkflowStoreDep,
    user: CurrentUserDep,
    owner_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ApiResponse[list[WorkflowData]]:
    """列表：普通用户强制只看自己；管理员默认全部，``?owner_id=`` 可缩到某归属。"""
    records = await store.list(
        owner_id=owner_filter_of(user, owner_id), limit=limit, offset=offset
    )
    return ApiResponse(data=[WorkflowData.from_record(item) for item in records])


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    store: WorkflowStoreDep,
    user: CurrentUserDep,
) -> ApiResponse[WorkflowData]:
    """定义详情；不存在 / 是别人的统一 404。"""
    record = await get_in_scope(store, user, workflow_id)
    return ApiResponse(data=WorkflowData.from_record(record))


@router.patch("/{workflow_id}")
async def rename_workflow(
    workflow_id: str,
    payload: RenameWorkflowRequest,
    request: Request,
    store: WorkflowStoreDep,
    user: CurrentUserDep,
) -> ApiResponse[WorkflowData]:
    """改名；同归属同名 409。"""
    record = await get_in_scope(store, user, workflow_id)
    try:
        updated = await store.rename(record.id, payload.name)
    except WorkflowNameConflict as exc:
        raise _conflict(exc) from exc
    if updated is None:  # get_in_scope 已确认存在；并发被删时走 404
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没有这个工作流", status_code=status.HTTP_404_NOT_FOUND
        )
    _audit(
        "工作流已改名",
        owner_id=record.owner_id,
        workflow_id=record.id,
        name=payload.name,
        trace_id=trace_id_of(request),
    )
    return ApiResponse(data=WorkflowData.from_record(updated), trace_id=trace_id_of(request))


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(
    workflow_id: str,
    request: Request,
    store: WorkflowStoreDep,
    user: CurrentUserDep,
) -> Response:
    """删除定义及其全部版本。"""
    record = await get_in_scope(store, user, workflow_id)
    removed = await store.delete(record.id)
    if not removed:
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没有这个工作流", status_code=status.HTTP_404_NOT_FOUND
        )
    _audit(
        "工作流已删除",
        owner_id=record.owner_id,
        workflow_id=record.id,
        name=record.name,
        trace_id=trace_id_of(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- 暂存
@router.put("/{workflow_id}/draft")
async def save_draft(
    workflow_id: str,
    payload: SaveDraftRequest,
    request: Request,
    store: WorkflowStoreDep,
    user: CurrentUserDep,
) -> ApiResponse[WorkflowData]:
    """暂存编辑中的图：**不校验业务内容**（半张图也能存），只要求顶层是 {nodes, edges}。

    存完当前指针切到 ``draft``；之后编辑器打开默认加载暂存区。提交版本走 POST versions。
    """
    record = await get_in_scope(store, user, workflow_id)
    trace_id = trace_id_of(request)
    try:
        graph_json = canonical_draft_json(payload.graph)
    except ValidationError as exc:
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            f"暂存图格式不合法：{exc.errors()[0].get('msg', '类型错误') if exc.errors() else '解析失败'}",
            status_code=HttpStatus.UNPROCESSABLE_ENTITY,
        ) from exc

    updated = await store.save_draft(record.id, graph_json)
    if updated is None:  # get_in_scope 已确认存在；并发被删时走 404
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没有这个工作流", status_code=status.HTTP_404_NOT_FOUND
        )
    _audit(
        "工作流已暂存",
        owner_id=record.owner_id,
        workflow_id=record.id,
        trace_id=trace_id,
    )
    return ApiResponse(data=WorkflowData.from_record(updated), trace_id=trace_id)


@router.get("/{workflow_id}/draft")
async def get_draft(
    workflow_id: str,
    store: WorkflowStoreDep,
    user: CurrentUserDep,
) -> ApiResponse[WorkflowDraftData]:
    """读暂存区；从没暂存过时 ``graph`` 为 null（200，不算错误）。"""
    record = await get_in_scope(store, user, workflow_id)
    return ApiResponse(data=WorkflowDraftData.from_record(record))


# --------------------------------------------------------------------------- 版本
@router.post("/{workflow_id}/versions")
async def save_version(
    workflow_id: str,
    payload: SaveVersionRequest,
    request: Request,
    store: WorkflowStoreDep,
    user: CurrentUserDep,
) -> Response:
    """保存版本：**先校验，过了才写库**；不过返回 200 + 校验报告（本次不产生任何数据）。

    路由默认状态码不钉死：校验失败是业务结果（200 + ``valid:false``），真正存了新版本
    才回 201（在成功分支用 JSONResponse 显式给）。
    """
    record = await get_in_scope(store, user, workflow_id)
    trace_id = trace_id_of(request)

    report = validate_graph(payload.graph)
    if not report.valid:
        api_logger().warning(
            "工作流保存被校验拦下",
            owner_id=record.owner_id,
            workflow_id=record.id,
            stage=report.stage,
            error_count=len(report.errors),
            trace_id=trace_id,
        )
        # 业务结果而非请求错误：200 + {valid:false,...}，前端按节点画红点
        return JSONResponse(
            ApiResponse(
                data=ValidationReportData.from_report(report), trace_id=trace_id
            ).model_dump(mode="json"),
            status_code=status.HTTP_200_OK,
        )

    # 校验过了再按各节点注册的默认值补全 config（trigger/level/timeout 之类），
    # 落库快照与摘要都以补全后的图为准——缺什么运行期不用再猜
    normalized = apply_config_defaults(payload.graph)
    version, created = await store.add_version(
        record,
        graph_json=canonical_graph_json(normalized),
        checksum=graph_checksum(normalized),
        note=payload.note,
    )
    latest = await store.get(record.id)
    if latest is None:  # add_version 刚写过；只可能在并发删除时落空
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没有这个工作流", status_code=status.HTTP_404_NOT_FOUND
        )
    _audit(
        "工作流版本已保存" if created else "工作流保存命中已有版本（内容未变）",
        owner_id=record.owner_id,
        workflow_id=record.id,
        version=version.version,
        created=created,
        trace_id=trace_id,
    )
    result = SaveVersionResultData(
        workflow=WorkflowData.from_record(latest),
        version=WorkflowVersionData.from_record(version),
        created=created,
    )
    return JSONResponse(
        ApiResponse(data=result, trace_id=trace_id).model_dump(mode="json"),
        # 真的产生了新版本才算 201；内容没变命中已有版本是 200（没创建任何东西）
        status_code=(
            status.HTTP_201_CREATED if created else status.HTTP_200_OK
        ),
    )


@router.get("/{workflow_id}/versions")
async def list_versions(
    workflow_id: str,
    store: WorkflowStoreDep,
    user: CurrentUserDep,
) -> ApiResponse[list[WorkflowVersionData]]:
    """版本历史（版本号倒序；不含图内容会过大？——当前规模直接带图，将来可加 ?brief=1）。"""
    record = await get_in_scope(store, user, workflow_id)
    versions = await store.list_versions(record.id)
    return ApiResponse(data=[WorkflowVersionData.from_record(item) for item in versions])


@router.get("/{workflow_id}/versions/{version}")
async def get_version(
    workflow_id: str,
    version: int,
    store: WorkflowStoreDep,
    user: CurrentUserDep,
) -> ApiResponse[WorkflowVersionData]:
    """某个版本的图快照；版本不存在 404。"""
    record = await get_in_scope(store, user, workflow_id)
    snapshot = await store.get_version(record.id, version)
    if snapshot is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            f"没有版本 {version}",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return ApiResponse(data=WorkflowVersionData.from_record(snapshot))


@router.post("/{workflow_id}/publish")
async def publish_workflow(
    workflow_id: str,
    payload: PublishRequest,
    request: Request,
    store: WorkflowStoreDep,
    user: CurrentUserDep,
) -> ApiResponse[WorkflowData]:
    """发布版本：不传 ``version`` 就发布当前最新版本；没存过任何版本 -> 409。

    发布**只挪发布指针**（status=published + published_version），不在请求里执行图——
    时间触发的注册在服务启动时统一做（载入所有已发布工作流），避免发布动作本身产生
    一次副作用执行；改了 cron 等触发配置后重启服务即生效。
    """
    record = await get_in_scope(store, user, workflow_id)
    target_version = payload.version if payload.version is not None else record.current_version
    if target_version == 0:
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            "还没有可发布的版本，请先提交图",
            status_code=status.HTTP_409_CONFLICT,
        )
    published = await store.publish(record.id, target_version)
    if published is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            f"没有版本 {target_version}",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    _audit(
        "工作流版本已发布",
        owner_id=record.owner_id,
        workflow_id=record.id,
        version=target_version,
        trace_id=trace_id_of(request),
    )
    return ApiResponse(data=WorkflowData.from_record(published), trace_id=trace_id_of(request))
