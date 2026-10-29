"""知识库管理接口。

入库属于内容管理操作，只对管理员开放：普通用户能查资料，但不能改资料。
"""

from fastapi import APIRouter, Header, HTTPException

from backend.schemas import ErrorResponse, IngestRequest, IngestResponse, KnowledgeListResponse
from backend.services import chat as chat_service
from backend.services import ingest as ingest_service
from backend.services import knowledge
from backend.services.ingest_leases import IngestBusyError, IngestLeaseLostError

knowledge_router = APIRouter()


def _require_admin(authorization: str) -> dict:
    """校验管理员身份，非管理员一律拒绝。"""
    user = chat_service.current_user(authorization)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


@knowledge_router.post(
    "/knowledge/ingest", response_model=IngestResponse, summary="提交文本资料入库",
    description="仅管理员可写入 JSON 文本资料。按规范化标题与正文计算指纹；重复返回 created=false、chunks=0。",
    responses={
        403: {"model": ErrorResponse, "description": "未登录或不具备管理员权限"},
        409: {
            "model": ErrorResponse, "description": "相同资料正在处理或入库租约已失效，请稍后重试",
            "headers": {"Retry-After": {"description": "建议至少等待的秒数，不保证届时已处理完成", "schema": {"type": "integer"}}},
        },
        422: {"model": ErrorResponse, "description": "输入校验失败或入库参数不合法"},
    },
)
def ingest_knowledge(
    req: IngestRequest, authorization: str = Header(default="")
) -> IngestResponse:
    """把一份资料写入知识库。

    同一份内容重复提交会返回已有记录（``created=false``），不会产生
    重复知识；因此前端重试或用户重复点击都是安全的。
    """
    _require_admin(authorization)
    try:
        result = ingest_service.ingest(
            title=req.title,
            content=req.content,
            kind=req.kind,
            keywords=req.keywords,
        )
    except (IngestBusyError, IngestLeaseLostError) as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers={"Retry-After": "1"}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return IngestResponse(**result.to_dict())


@knowledge_router.get(
    "/knowledge/items", response_model=KnowledgeListResponse, summary="列出知识条目摘要",
    responses={403: {"model": ErrorResponse, "description": "未登录或不具备管理员权限"}},
)
def list_knowledge(authorization: str = Header(default="")) -> dict:
    """列出已入库的资料，供管理端核对。"""
    _require_admin(authorization)
    db_items = knowledge._all_docs()
    return {
        "total": len(db_items),
        "items": [
            {"id": i["id"], "title": i["title"], "kind": i["kind"]}
            for i in db_items
        ],
    }
