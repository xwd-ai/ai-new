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


class LogoutRequest(BaseModel):
    """登出请求：访问令牌走 Authorization 头，续期令牌可选放在请求体。

    续期令牌不是必填：客户端只拿得到访问令牌时也能退出，
    只是那条长期凭证会留到自然过期。给了就一并吊销，登出更彻底。
    """

    refresh_token: str = Field(
        default="",
        max_length=4096,
        description="登录或换发时拿到的续期令牌；留空表示只作废访问令牌",
    )


class LogoutResponse(BaseModel):
    """登出响应：说明本次作废了哪些凭证。

    revoked_access / revoked_refresh 表示本次是否新增作废记录，
    重复登出时为 False，语义上仍是成功；access_expires_at 是令牌原本的
    到期时间，便于客户端判断本地缓存何时可以彻底丢弃。
    """

    message: str
    user: dict = Field(default_factory=dict, description="登出前确认的身份，账号已删则为空")
    revoked_access: bool = Field(..., description="访问令牌本次是否新登记为作废")
    access_expires_at: int = Field(..., description="作废的访问令牌原到期时间戳（秒）")
    session_removed: bool = Field(..., description="是否同时删除了对应的会话记录")
    revoked_refresh: bool = Field(..., description="续期令牌本次是否被吊销")
    refresh_expires_at: Optional[int] = Field(
        default=None, description="被吊销的续期令牌原到期时间戳（秒），未提交则为空"
    )


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


class CleanupRequest(BaseModel):
    """过期凭据清理请求：两个参数都有默认值，空请求体即按默认口径清理。

    retention 是过期之后额外保留的秒数，默认 0 表示到期即删；
    排查「刚过期就登不上」这类问题时可以留一段观察窗口再清。
    limit 控制单表单次删除上限，积压较多时分多次调用，避免一次写事务过大。
    """

    retention: int = Field(
        default=0,
        ge=0,
        le=30 * 24 * 3600,
        description="过期后额外保留的秒数，默认 0（到期即删）",
    )
    limit: int = Field(
        default=2000,
        ge=1,
        le=10000,
        description="单表单次删除上限",
    )


class CleanupResponse(BaseModel):
    """清理结果：逐表条数之和为 total，truncated 为真表示可能还有剩余。

    cutoff 是本次判定过期的截止时间戳：早于它的记录都被视作失效，
    便于从日志里对上一次清理的口径，不必回推 retention 的换算。
    """

    message: str
    deleted: dict = Field(..., description="各表删除条数：sessions / refresh_tokens / revoked_tokens")
    total: int = Field(..., description="本次删除总条数")
    cutoff: int = Field(..., description="过期判定的截止时间戳（秒）")
    retention: int = Field(..., description="本次生效的保留窗口（秒）")
    truncated: bool = Field(..., description="是否因达到单次上限而可能仍有剩余")
