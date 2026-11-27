"""知识库管理接口。

入库属于内容管理操作，只对管理员开放：普通用户能查资料，但不能改资料。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from backend.schemas import KnowledgeDiagnosisResponse, ErrorResponse, IngestRequest, IngestResponse
from backend.services import auth as auth_service
from backend.services import ingest as ingest_service
from backend.services import knowledge
from backend.services.ingest_leases import IngestBusyError, IngestLeaseLostError


def _require_admin(authorization: str = Header(default="")) -> dict:
    """复用统一管理员依赖，保留知识库的 403 detail 错误契约。"""
    try:
        return auth_service.require_admin(auth_service.resolve_authorization(authorization), "执行该操作")
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=403, detail="需要管理员权限") from exc


knowledge_router = APIRouter(dependencies=[Depends(_require_admin)])


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
    req: IngestRequest,
) -> IngestResponse:
    """把一份资料写入知识库。

    同一份内容重复提交会返回已有记录（``created=false``），不会产生
    重复知识；因此前端重试或用户重复点击都是安全的。
    """
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


@knowledge_router.post(
    '/knowledge/ingest/recover/{fingerprint}', response_model=IngestResponse,
    summary='从已持久化阶段恢复一份资料入库',
    responses={403: {'model': ErrorResponse}, 404: {'model': ErrorResponse},
               409: {'model': ErrorResponse}, 422: {'model': ErrorResponse}},
)
def recover_ingest(fingerprint: str) -> dict:
    try:
        return ingest_service.recover_ingest(fingerprint).to_dict()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail='未找到入库 journal') from exc
    except (IngestBusyError, IngestLeaseLostError) as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers={'Retry-After': '1'}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail='入库 journal 无法恢复，请人工核对持久化输入与阶段') from exc


@knowledge_router.get(
    "/knowledge/diagnose", response_model=KnowledgeDiagnosisResponse, summary="诊断知识检索",
    responses={403: {"model": ErrorResponse, "description": "未登录或不具备管理员权限"}},
)
def diagnose_knowledge(
    q: str,
    top_k: Annotated[int, Query(description="实际截断到 1～20，不因整数超出范围返回 422")] = 3,
) -> dict:
    """诊断一次检索，返回每个阶段的命中情况。

    用于排查「为什么搜不到 / 为什么返回的是这些」：直接看出是没召回、
    被融合挤掉、被阈值拦下，还是被去冗余合并了。

    管理员专属：它会把内部标识与打分过程暴露出来，不适合给普通用户。
    """
    provider = knowledge.configured_embedding()
    options = {"embedding_provider": provider} if provider is not None else {}
    return knowledge.diagnose(q, top_k=max(1, min(top_k, 20)), **options)
