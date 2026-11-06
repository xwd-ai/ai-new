from pydantic import BaseModel, Field, field_validator

# 允许的知识类型，与 ingest 层的校验保持一致
ALLOWED_KINDS = {"manual", "faq", "rule"}


class IngestRequest(BaseModel):
    """资料入库请求。

    正文允许较长（成篇的手册/规则），但对单次请求设上限，避免一次提交
    占用过多内存与存储；更大规模的内容应当走文件上传而不是纯文本提交。
    """

    title: str = Field(..., min_length=1, max_length=200, description="资料标题")
    content: str = Field(..., min_length=1, max_length=100_000, description="资料正文")
    kind: str = Field(default="manual", description="资料类型：manual / faq / rule")
    keywords: list[str] = Field(default_factory=list, description="关键词，留空则自动提取")

    @field_validator("title")
    @classmethod
    def _validate_title(cls, v: str) -> str:
        text = (v or "").strip()
        if not text:
            raise ValueError("标题不能为空")
        return text

    @field_validator("content")
    @classmethod
    def _validate_content(cls, v: str) -> str:
        text = (v or "").strip()
        if not text:
            raise ValueError("正文不能为空")
        return text

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, v: str) -> str:
        kind = (v or "").strip().lower()
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"类型必须是 {'/'.join(sorted(ALLOWED_KINDS))} 之一")
        return kind

    @field_validator("keywords")
    @classmethod
    def _validate_keywords(cls, v: list[str]) -> list[str]:
        if not v:
            return []
        cleaned: list[str] = []
        for item in v:
            if not isinstance(item, str):
                raise ValueError("关键词必须是字符串")
            token = item.strip()
            if token and token not in cleaned:
                cleaned.append(token)
        if len(cleaned) > 20:
            raise ValueError("关键词最多 20 个")
        return cleaned


class IngestResponse(BaseModel):
    """入库结果。

    ``created`` 区分「新建」与「已存在」：重复提交同一份内容不会报错，
    但调用方需要知道这次有没有真的写入新数据。
    """

    knowledge_id: str
    created: bool
    chunks: int
    stage: str
    detail: str = ""


class KnowledgeSummary(BaseModel):
    id: str
    title: str
    kind: str


class KnowledgeListResponse(BaseModel):
    total: int
    items: list[KnowledgeSummary]


class KnowledgeDiagnosisResponse(BaseModel):
    query: str
    top_k: int = Field(description="实际使用的返回条数，输入会截断到 1～20")
    prefetch_depth: int
    config: dict = Field(description="本次检索配置")
    stages: list[dict] = Field(description="各检索阶段的数量、条目编号与诊断信息")
    final_ids: list[str]


class PendingIngest(BaseModel):
    fingerprint: str
    knowledge_id: str
    stage: str
    chunk_count: int
    updated_at: float


class PendingIngestResponse(BaseModel):
    items: list[PendingIngest]
    total: int | None = Field(default=None, description='传入分页参数时返回全部待恢复记录数')
