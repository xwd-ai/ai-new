"""管理页可调整且已接入检索链路的参数。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RetrievalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    prefetch_floor: int = Field(ge=1, le=1000, description="候选预取下限")
    prefetch_multiplier: int = Field(ge=1, le=20, description="目标条数的候选预取倍数")
    rrf_k: int = Field(ge=1, le=1000, description="RRF 排名平滑值")
    vector_weight: float = Field(ge=0, le=1, description="向量召回权重")
    keyword_weight: float = Field(ge=0, le=1, description="关键词召回权重")
    mmr_lambda: float = Field(ge=0, le=1, description="MMR 相关性权重，越小越偏好多样性")

    @model_validator(mode="after")
    def require_recall_weight(self):
        if self.vector_weight + self.keyword_weight <= 0:
            raise ValueError("两路召回权重不能同时为 0")
        return self
