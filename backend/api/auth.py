"""认证接口：登录、当前用户、注册。

约定：
- 业务失败一律抛 AuthError 派生异常，由 AuthErrorRoute 统一转成
  {error, error_code, error_detail?} 响应，接口内不拼装错误字典；
- 登录沿用既有契约：失败也返回 200，token 为空串、user 为空对象，
  前端据此区分「凭证错误」与「接口异常」；
- 登录成功返回的是签名的 access token，附带令牌类型与有效期，
  失败分支不返回这些字段，响应体与旧契约完全一致。
"""

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from backend.schemas.auth import (
    LoginRequest,
    LoginResponse,
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


@auth_router.get("/me")
def me(authorization: str = Header(default="")):
    token = authorization.replace("Bearer ", "").strip()
    session = auth.get_session(token)
    if not session:
        return {"user": None}
    user = auth.find_by_username(session["username"])
    return {"user": auth.public_user(user) if user else None}
