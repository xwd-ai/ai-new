from fastapi import APIRouter, Header
from pydantic import BaseModel

from backend import auth

auth_router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user: dict


@auth_router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest):
    user = auth.authenticate(req.username, req.password)
    if not user:
        return {"token": "", "user": {}}
    token = auth.create_session(user)
    return {"token": token, "user": auth.public_user(user)}


@auth_router.get("/me")
def me(authorization: str = Header(default="")):
    token = authorization.replace("Bearer ", "").strip()
    session = auth.get_session(token)
    if not session:
        return {"user": None}
    user = auth.find_by_username(session["username"])
    return {"user": auth.public_user(user) if user else None}
