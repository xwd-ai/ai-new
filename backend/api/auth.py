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
- 身份与角色统一由依赖函数声明：current_user 允许匿名、require_login 要求登录、
  require_admin 要求管理员，接口内不再自己比对角色字符串。
  过期凭据清理是运维动作，只对管理员开放：未登录 401、非管理员 403。
"""

import re
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, Request
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


# ---------------------------------------------------------------------------
# 错误响应契约：认证、订单、工单三个模块共用同一套字段与同一个组装入口
# ---------------------------------------------------------------------------
#
# 各模块的业务异常统一携带 code / message / details / http_status 四项：
# code 是机器可读标识，message 是可直接展示的文案，details 是可选补充上下文，
# http_status 是建议的响应码。响应体固定为
#   {"error": 文案, "error_code": 错误码, "error_detail": 补充上下文（可选）}
# 且只由 build_error_body 一处组装，路由类不再各自拼字典。
# 错误码约定为大写字母开头、只含大写字母/数字/下划线的 <模块>_<原因> 形式，
# 因此前端可以按 error_code 前缀区分来源，按整体值分支处理。

ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def is_valid_error_code(code: str) -> bool:
    """判断错误码是否符合对外约定，供用例与自检使用。"""
    return bool(ERROR_CODE_PATTERN.match(code or ""))


def build_error_body(
    code: str, message: str = "", details: Optional[dict] = None
) -> dict:
    """组装错误响应体：字段顺序固定，补充上下文只在有值时出现。

    details 复制一份再放入，避免调用方后续改动异常对象时影响已生成的响应。
    """
    body = {"error": message or "", "error_code": code}
    if details:
        body["error_detail"] = dict(details)
    return body


def error_payload(exc: Exception) -> dict:
    """把业务异常序列化为错误响应体。

    认证异常历史上用 error_code/detail 两个字段名，工单与订单用 code/details，
    认证异常类已把两套名字对齐，这里按同一组属性取值，不再按模块分支。
    """
    return build_error_body(
        getattr(exc, "code", ""),
        getattr(exc, "message", ""),
        getattr(exc, "details", None),
    )


def error_response(exc: Exception) -> JSONResponse:
    """按异常自带的状态码与统一结构生成错误响应。"""
    return JSONResponse(
        status_code=getattr(exc, "http_status", 500), content=error_payload(exc)
    )


class BusinessErrorRoute(APIRoute):
    """业务异常统一收口的路由基类。

    子类只声明本模块要接管的异常族 error_types，必要时覆写 translate_error
    把上游异常翻译成本模块的错误码；响应码与响应体形状一律由本类决定，
    新增接口不必再各写一遍 try/except，错误结构也不会随接口各自漂移。
    """

    #: 本路由要转换为错误响应的异常类型，为空表示不接管任何业务异常。
    error_types: tuple = ()

    def translate_error(self, exc: Exception):
        """把捕获到的异常换成对外异常，返回 None 表示不接管、按原样抛出。"""
        return exc

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def dispatch(request: Request):
            try:
                return await handler(request)
            except self.error_types as exc:
                translated = self.translate_error(exc)
                if translated is None:
                    raise
                return error_response(translated)

        return dispatch


class AuthErrorRoute(BusinessErrorRoute):
    """认证接口：认证异常原样转成统一错误响应，不改写错误码。"""

    error_types = (auth.AuthError,)


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


# ---------------------------------------------------------------------------
# 权限依赖：接口只声明需要什么身份，判定与拦截都交给认证服务
# ---------------------------------------------------------------------------
#
# 三个依赖按「匿名 → 已登录 → 管理员」逐级收紧，路由按需选用即可：
# 判定口径只有认证服务一处，接口层不再自己比对角色字符串，
# 也就不会出现某个接口把 "Admin" 当成管理员、或漏判整个角色校验的情况。
# 依赖里抛的异常同样由 AuthErrorRoute 统一转成错误响应。


def current_user(authorization: str = Header(default="")) -> Optional[dict]:
    """依赖：解析当前登录用户，未登录返回 None。

    供「匿名也能访问、登录后行为不同」的接口使用；
    需要强制登录的接口改用 require_login，让未登录在入口就被拒绝。
    """
    return auth.resolve_authorization(authorization)


def require_login(authorization: str = Header(default="")) -> dict:
    """依赖：要求已登录，未登录按 401 拒绝。"""
    return auth.require_login(auth.resolve_authorization(authorization))


def require_customer(authorization: str = Header(default="")) -> dict:
    """依赖：要求客户账号，管理员账号走这条分支会被 403 拒绝。

    用于只对客户开放的自助操作（如本人资料、本人订单），
    把「客户能做什么」与「管理员能做什么」分开声明，而不是靠路由各自注解。
    """
    return auth.require_role(
        auth.resolve_authorization(authorization), auth.CUSTOMER_ROLE
    )


def require_admin(authorization: str = Header(default="")) -> dict:
    """依赖：要求管理员身份，未登录 401、非管理员 403。

    清理、批处理这类会改动他人数据的运维动作只声明这一个依赖即可；
    身份取自令牌并经回库确认，不依赖令牌里的角色快照。
    """
    return auth.require_admin(auth.resolve_authorization(authorization), "执行该操作")


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
    user: dict = Depends(require_admin),
):
    """删除已过期的会话、续期登记与作废记录，返回逐表条数。

    全部参数可选：空请求体按「到期即删、单表 2000 条」的默认口径执行。
    返回体的 truncated 为真时说明仍有剩余，再次调用即可继续清理。
    权限由 require_admin 依赖把关：未登录 401、非管理员 403，进入函数时
    已是管理员身份，函数体内不再重复判断。
    """
    params = req or CleanupRequest()
    return auth.purge_expired_sessions(retention=params.retention, limit=params.limit)


@auth_router.get("/me")
def me(user: Optional[dict] = Depends(current_user)):
    """返回当前登录用户；令牌缺失或校验失败时 user 为 null。

    身份取自令牌声明并经回库确认，账号被删除后旧令牌即失效，
    不会因为令牌仍在有效期内而返回一个已不存在的用户。
    """
    return {"user": auth.public_user(user) if user else None}
