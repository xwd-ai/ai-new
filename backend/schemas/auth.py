from typing import Optional

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    """登录响应：令牌与公开用户信息。

    token_type / expires_in / expires_at 只在登录成功时出现（失败时为 null，
    由接口层按「不输出空字段」序列化），因此凭证错误场景的响应体仍是
    {"token": "", "user": {}} 这一既有形态。refresh_token 及其有效期同理，
    失败分支不返回任何续期字段。
    """

    token: str
    user: dict
    token_type: Optional[str] = Field(default=None, description="令牌类型，固定 Bearer")
    expires_in: Optional[int] = Field(default=None, description="令牌剩余有效秒数")
    expires_at: Optional[int] = Field(default=None, description="令牌到期时间戳（秒）")
    refresh_token: Optional[str] = Field(default=None, description="续期用的长期令牌")
    refresh_expires_in: Optional[int] = Field(default=None, description="refresh token 剩余有效秒数")
    refresh_expires_at: Optional[int] = Field(default=None, description="refresh token 到期时间戳（秒）")


class RefreshRequest(BaseModel):
    """换发请求：续期令牌既可以放在请求体，也可以走 Authorization 头。

    两种传法都支持是为了兼容不同客户端习惯：网页端通常读本地存储后放请求体，
    脚本与代理环境更习惯沿用 Authorization。两处都为空时按未携带令牌处理。
    """

    refresh_token: str = Field(
        default="",
        max_length=4096,
        description="登录时下发的续期令牌；留空时从 Authorization 头读取",
    )


class RefreshResponse(BaseModel):
    """换发响应：与登录成功时的令牌字段同名同义，客户端用同一套逻辑更新本地状态。

    refresh_token 为原样回传的续期凭证（不轮换），其有效期仍以首次登录时为准，
    因此 refresh_expires_in 只会随使用次数递减，不会因为换发而重置。
    """

    token: str
    user: dict
    token_type: str = Field(default="Bearer", description="令牌类型")
    expires_in: int = Field(..., description="新访问令牌的剩余有效秒数")
    expires_at: int = Field(..., description="新访问令牌的到期时间戳（秒）")
    refresh_token: str = Field(..., description="原样回传的续期令牌")
    refresh_expires_in: int = Field(..., description="续期令牌的剩余有效秒数")
    refresh_expires_at: int = Field(..., description="续期令牌的到期时间戳（秒）")


class RegisterRequest(BaseModel):
    """注册请求：只约束入参结构，用户名格式与密码强度交给服务层判定。

    分层理由：结构性错误（缺字段、超长文本）由框架返回 422 detail 列表；
    业务性错误（用户名被占用、密码强度不足）由服务层抛业务异常，
    两者响应体不同，前端可分别提示。
    """

    username: str = Field(..., max_length=64, description="登录名，3–32 位，字母开头")
    password: str = Field(..., max_length=128, description="明文密码，仅在本次请求内用于哈希")
    name: str = Field(default="", max_length=32, description="显示名称，留空时取用户名")

    @field_validator("username", "name")
    @classmethod
    def _strip_text(cls, v: str) -> str:
        # 密码不做去空白处理：空白本身由强度规则判定，避免静默改写用户输入
        return (v or "").strip()


class RegisterResponse(BaseModel):
    user: dict
    message: str
