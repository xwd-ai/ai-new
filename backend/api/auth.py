"""认证接口：登录、换发、当前用户、注册。

约定：
- 业务失败一律抛 AuthError 派生异常，由 AuthErrorRoute 统一转成
  {error, error_code, error_detail?} 响应，接口内不拼装错误字典；
- 登录沿用既有契约：失败也返回 200，token 为空串、user 为空对象，
  前端据此区分「凭证错误」与「接口异常」；
- 登录成功返回签名的 access token 与长期 refresh token，附带各自的
  有效期，失败分支不返回这些字段，响应体与旧契约完全一致；
- 换发走严格语义：续期令牌缺失、无效、吊销、过期或账号已删都返回 401
  与稳定的错误码，不像登录那样用空 token 表示失败——换发失败意味着
  客户端手里的凭证彻底不能用了，应当直接回登录页。
- 登出把访问令牌记入作废名单并删除会话记录，续期令牌提交了才吊销；
  作废之后同一张令牌再来访问一律按未登录处理，直到它自然过期。
- 过期凭据清理是运维动作，只对管理员开放：未登录 401、非管理员 403。
"""

from typing import Optional

from fastapi import APIRouter, Body, Header, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from backend.schemas.auth import (
    CleanupRequest,
    CleanupResponse,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    LogoutResponse,
    RefreshRequest,
    RefreshResponse,
    RegisterRequest,
    RegisterResponse,
)
from backend.services import auth


class AuthErrorRoute(APIRoute):
    """把认证业务异常统一转换为错误响应。

    响应体固定为 {"error": 文案, "error_code": 错误码, "error_detail": 补充上下文（可选）}，
    与工单模块的错误结构保持一致，前端按 error_code 分支即可。
    """

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def dispatch(request: Request):
            try:
                return await handler(request)
            except auth.AuthError as exc:
                return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

        return dispatch


auth_router = APIRouter(route_class=AuthErrorRoute)


def _header_token(authorization: str) -> str:
    """从 Authorization 头取令牌，头为空时返回空串（由调用方统一报缺失）。

    标准写法是 Bearer <token>；只给令牌原文也接受，便于手工联调。
    头部内容原样交给服务层校验，非法内容在那里按令牌格式错误拒绝，
    接口层不再重复判断一遍格式。
    """
    try:
        return auth.extract_bearer_token(authorization)
    except auth.TokenMissingError:
        return ""
    except auth.TokenMalformedError:
        raw = (authorization or "").strip()
        head = raw.split(None, 1)[0].lower() if raw else ""
        # 写了 Bearer 却没跟令牌，仍按未携带处理；其它内容当作裸令牌交给服务层校验
        return "" if head == "bearer" else raw


def _require_admin(authorization: str) -> dict:
    """取当前用户并要求管理员身份，未登录 401、非管理员 403。

    清理会删除表记录，属于运维动作，不开放给普通账号；
    身份同样取自令牌并经回库确认，不依赖令牌里的角色快照。
    """
    user = auth.resolve_authorization(authorization)
    if not user:
        raise auth.TokenMissingError("未登录")
    if user.get("role") != "admin":
        raise auth.PermissionDeniedError("仅管理员可执行该操作")
    return user


@auth_router.post("/login", response_model=LoginResponse, response_model_exclude_none=True)
def login(req: LoginRequest):
    user = auth.authenticate(req.username, req.password)
    if not user:
        return {"token": "", "user": {}}
    issued = auth.issue_login_token(user)
    return {**issued, "user": auth.public_user(user)}


@auth_router.post(
    "/register",
    response_model=RegisterResponse,
    summary="注册客户账号",
    responses={
        409: {"description": "用户名已被占用"},
        422: {"description": "用户名格式或密码强度不符合要求"},
    },
)
def register(req: RegisterRequest):
    """注册客户账号：身份固定为 customer，管理员账号不经由此接口创建。"""
    user = auth.register_user(req.username, req.password, req.name, role="customer")
    return {"user": user, "message": "注册成功"}


@auth_router.post(
    "/refresh",
    response_model=RefreshResponse,
    summary="用续期令牌换发访问令牌",
    responses={
        401: {"description": "续期令牌缺失、无效、已吊销、已过期或账号已删除"},
    },
)
def refresh(
    req: Optional[RefreshRequest] = Body(default=None),
    authorization: str = Header(default=""),
):
    """用长期续期令牌换一张新的访问令牌。

    令牌先取请求体，请求体没给再取 Authorization 头；两处都空时按未携带令牌拒绝。
    返回体与登录成功时的令牌字段同名同义，客户端可直接复用解析逻辑。
    """
    token = (req.refresh_token if req else "") or _header_token(authorization)
    if not token:
        raise auth.TokenMissingError("缺少续期令牌")
    return auth.refresh_access_token(token)


@auth_router.post(
    "/logout",
    response_model=LogoutResponse,
    summary="登出并作废当前令牌",
    responses={
        401: {"description": "未携带令牌、令牌无效，或续期令牌不属于当前登录用户"},
    },
)
def logout(
    req: Optional[LogoutRequest] = Body(default=None),
    authorization: str = Header(default=""),
):
    """登出：作废当前访问令牌，可选一并吊销续期令牌。

    访问令牌从 Authorization 头取，缺失即按未登录拒绝——没有令牌可作废时
    返回成功只会让客户端误以为本地状态已经清干净。
    已经过期的访问令牌仍可登出，客户端换发失败后需要一条退出的路。
    重复登出返回同样的成功语义，前端不必为「第二次退出」写分支。
    """
    token = _header_token(authorization)
    if not token:
        raise auth.TokenMissingError("缺少认证令牌")
    return auth.logout(token, req.refresh_token if req else "")


@auth_router.post(
    "/sessions/cleanup",
    response_model=CleanupResponse,
    summary="清理过期登录凭证",
    responses={
        401: {"description": "未携带令牌或令牌无效"},
        403: {"description": "非管理员账号"},
    },
)
def cleanup_sessions(
    req: Optional[CleanupRequest] = Body(default=None),
    authorization: str = Header(default=""),
):
    """删除已过期的会话、续期登记与作废记录，返回逐表条数。

    全部参数可选：空请求体按「到期即删、单表 2000 条」的默认口径执行。
    返回体的 truncated 为真时说明仍有剩余，再次调用即可继续清理。
    """
    _require_admin(authorization)
    params = req or CleanupRequest()
    return auth.purge_expired_sessions(retention=params.retention, limit=params.limit)


@auth_router.get("/me")
def me(authorization: str = Header(default="")):
    """返回当前登录用户；令牌缺失或校验失败时 user 为 null。

    身份取自令牌声明并经回库确认，账号被删除后旧令牌即失效，
    不会因为令牌仍在有效期内而返回一个已不存在的用户。
    """
    user = auth.resolve_authorization(authorization)
    return {"user": auth.public_user(user) if user else None}
