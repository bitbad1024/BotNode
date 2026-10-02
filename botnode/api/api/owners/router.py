"""归属清单的 HTTP 入口：给「按归属筛选」提供可选项。

    GET <prefix>/owners    归属清单（管理员 = 全部用户；普通用户 = 只有自己）

口径与工作流 / 机器人列表一致（见 :func:`botnode.api.api.onebot.dependencies.owner_filter_of`）：
普通用户问「有哪些归属」，答案只有他自己 —— 拿这份清单去筛，筛出来的仍是自己的内容，
**多一个接口不会因此多看得见别人的东西**（隔离始终在服务端按登录身份把关）。

没登录一律 401（:data:`~botnode.api.api.onebot.dependencies.CurrentUserDep`）。
"""
from __future__ import annotations

from fastapi import APIRouter, Request, status

from ...common.dependencies import trace_id_of
from ...common.models import ApiResponse, ErrorResponse
from ..onebot.dependencies import CurrentUserDep, UserStoreDep, is_admin
from .responses import OwnerData

router = APIRouter(prefix="/owners", tags=["归属"])


@router.get(
    "",
    response_model=ApiResponse[list[OwnerData]],
    summary="归属清单（管理员 = 全部用户）",
    responses={status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "没登录"}},
)
async def list_owners(
    request: Request, user: CurrentUserDep, users: UserStoreDep
) -> ApiResponse[list[OwnerData]]:
    """可选归属：管理员拿全量用户（按账号排序），普通用户只有自己那一条。"""
    if is_admin(user):
        records = await users.list_all()
    else:
        # 普通用户只有一个归属。查不到（账号被删了）就给空清单：前端见选项不足则不显示下拉。
        me = await users.get_by_id(user.user.id)
        records = [me] if me is not None else []
    return ApiResponse[list[OwnerData]](
        data=[
            OwnerData(owner_id=record.id, account=record.account, nickname=record.nickname)
            for record in records
        ],
        trace_id=trace_id_of(request),
    )
