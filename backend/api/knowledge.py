"""知识库管理接口。

入库属于内容管理操作，只对管理员开放：普通用户能查资料，但不能改资料。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query

from backend.schemas import ErrorResponse, IngestRequest, IngestResponse, KnowledgeListResponse
from backend.services import chat as chat_service
from backend.services import ingest as ingest_service
from backend.services import knowledge
from backend.services import retrieval_config
from backend.schemas.retrieval import RetrievalSettings
from backend.schemas.knowledge import PendingIngestResponse
from backend.services.ingest_leases import IngestBusyError, IngestLeaseLostError

knowledge_router = APIRouter()


def _require_admin(authorization: str) -> dict:
    """校验管理员身份，非管理员一律拒绝。"""
    user = chat_service.current_user(authorization)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


@knowledge_router.get(
    "/knowledge/settings", response_model=RetrievalSettings, summary="读取当前检索设置",
    responses={403: {"model": ErrorResponse, "description": "需要管理员权限"}},
)
def get_retrieval_settings(authorization: str = Header(default="")) -> RetrievalSettings:
    _require_admin(authorization)
    return retrieval_config.current_settings()


@knowledge_router.put(
    "/knowledge/settings", response_model=RetrievalSettings, summary="保存检索设置",
    description="完整提交六项参数，保存到本地配置文件；后续检索调用读取，不改变当前对话的关键词检索路径。",
    responses={
        403: {"model": ErrorResponse, "description": "需要管理员权限"},
        422: {"model": ErrorResponse, "description": "参数越界、类型错误或两路权重同时为零"},
        503: {"model": ErrorResponse, "description": "配置文件保存失败"},
    },
)
def put_retrieval_settings(
    settings: RetrievalSettings, authorization: str = Header(default=""),
) -> RetrievalSettings:
    _require_admin(authorization)
    try:
        return retrieval_config.save_settings(settings)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="检索设置保存失败，请检查配置目录写权限或稍后重试") from exc


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
    description="按资料编号升序列出摘要，可按标题子串与类型组合筛选。total 为筛选后的总数，支持可选分页。",
    responses={
        403: {"model": ErrorResponse, "description": "未登录或不具备管理员权限"},
        422: {"model": ErrorResponse, "description": "分页或筛选参数不合法"},
    },
)
def list_knowledge(
    authorization: str = Header(default=""),
    offset: Annotated[int, Query(ge=0, description="跳过的条目数")] = 0,
    limit: Annotated[int | None, Query(ge=1, le=100, description="最多返回条数，省略时返回剩余全部")] = None,
    title: Annotated[str, Query(max_length=200, description="标题字面子串，去除首尾空白后为空则不筛选")] = "",
    kind: Annotated[Literal["manual", "faq", "rule"] | None, Query(description="资料类型精确匹配")] = None,
) -> dict:
    """列出已入库的资料，供管理端核对。"""
    _require_admin(authorization)
    return knowledge.list_document_summaries(offset=offset, limit=limit, title=title, kind=kind)


@knowledge_router.get(
    '/knowledge/ingest/pending', response_model=PendingIngestResponse,
    summary='查看待恢复的入库记录（最多 100 条）',
    responses={403: {'model': ErrorResponse}},
)
def pending_ingests(authorization: str = Header(default='')) -> dict:
    _require_admin(authorization)
    return {'items': ingest_service.pending_ingests()}


@knowledge_router.post(
    '/knowledge/ingest/recover/{fingerprint}', response_model=IngestResponse,
    summary='从已持久化阶段恢复一份资料入库',
    responses={403: {'model': ErrorResponse}, 404: {'model': ErrorResponse},
               409: {'model': ErrorResponse}, 422: {'model': ErrorResponse}},
)
def recover_ingest(fingerprint: str, authorization: str = Header(default='')) -> dict:
    _require_admin(authorization)
    try:
        return ingest_service.recover_ingest(fingerprint).to_dict()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail='未找到入库 journal') from exc
    except (IngestBusyError, IngestLeaseLostError) as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers={'Retry-After': '1'}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail='入库 journal 无法恢复，请人工核对持久化输入与阶段') from exc
