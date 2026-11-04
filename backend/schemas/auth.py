from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user: dict


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
