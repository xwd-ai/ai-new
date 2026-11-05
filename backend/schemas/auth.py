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
