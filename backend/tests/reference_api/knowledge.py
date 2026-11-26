"""已迁往 Go 的知识 HTTP 契约，仅用于 Python 回归测试。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.knowledge import _require_admin, knowledge_router as active_router
from backend.schemas import ErrorResponse, KnowledgeListResponse
from backend.schemas.knowledge import PendingIngestResponse
from backend.schemas.retrieval import RetrievalSettings
from backend.services import auth as auth_service, ingest as ingest_service, knowledge, retrieval_config

migrated_router = APIRouter(dependencies=[Depends(_require_admin)])

@migrated_router.get(
    "/knowledge/settings", response_model=RetrievalSettings, summary="读取当前检索设置",
    responses={403: {"model": ErrorResponse, "description": "需要管理员权限"}},
)
def get_retrieval_settings() -> RetrievalSettings:
    return retrieval_config.current_settings()


@migrated_router.put(
    "/knowledge/settings", response_model=RetrievalSettings, summary="保存检索设置",
    description="完整提交六项参数，保存到本地配置文件；后续检索调用读取，不改变当前对话的关键词检索路径。",
    responses={
        403: {"model": ErrorResponse, "description": "需要管理员权限"},
        422: {"model": ErrorResponse, "description": "参数越界、类型错误或两路权重同时为零"},
        503: {"model": ErrorResponse, "description": "配置文件保存失败"},
    },
)
def put_retrieval_settings(
    settings: RetrievalSettings,
) -> RetrievalSettings:
    try:
        return retrieval_config.save_settings(settings)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="检索设置保存失败，请检查配置目录写权限或稍后重试") from exc


@migrated_router.get(
    "/knowledge/items", response_model=KnowledgeListResponse, summary="列出知识条目摘要",
    description="按资料编号升序列出摘要，可按标题子串与类型组合筛选。total 为筛选后的总数，支持可选分页。",
    responses={
        403: {"model": ErrorResponse, "description": "未登录或不具备管理员权限"},
        422: {"model": ErrorResponse, "description": "分页或筛选参数不合法"},
    },
)
def list_knowledge(
    offset: Annotated[int, Query(ge=0, description="跳过的条目数")] = 0,
    limit: Annotated[int | None, Query(ge=1, le=100, description="最多返回条数，省略时返回剩余全部")] = None,
    title: Annotated[str, Query(max_length=200, description="标题字面子串，去除首尾空白后为空则不筛选")] = "",
    kind: Annotated[Literal["manual", "faq", "rule"] | None, Query(description="资料类型精确匹配")] = None,
) -> dict:
    """列出已入库的资料，供管理端核对。"""
    return knowledge.list_document_summaries(offset=offset, limit=limit, title=title, kind=kind)


@migrated_router.get(
    '/knowledge/ingest/pending', response_model=PendingIngestResponse,
    response_model_exclude_none=True,
    summary='查看待恢复的入库记录',
    description='按更新时间和指纹升序返回。无参数时返回前 100 条，传入筛选或分页参数时附带匹配记录总数。',
    responses={403: {'model': ErrorResponse}, 422: {'model': ErrorResponse, 'description': '筛选或分页参数不合法'}},
)
def pending_ingests(
    offset: Annotated[int | None, Query(ge=0, description='跳过的记录数，省略时为 0')] = None,
    limit: Annotated[int | None, Query(ge=1, le=100, description='每页上限，省略时为 100')] = None,
    stage: Annotated[Literal['parsed', 'chunked'] | None, Query(description='按已保存输入或已完成分块筛选')] = None,
) -> dict:
    if offset is None and limit is None and stage is None:
        return {'items': ingest_service.pending_ingests()}
    return ingest_service.pending_ingest_page(
        offset=0 if offset is None else offset,
        limit=100 if limit is None else limit,
        stage=stage,
    )



knowledge_router = APIRouter()
knowledge_router.include_router(active_router)
knowledge_router.include_router(migrated_router)
