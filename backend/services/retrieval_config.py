"""检索参数配置。

检索链路上有一批可调参数（候选取多少、融合权重、阈值多严、去冗余多强）。
它们此前散落在各处的常量里，调整一次要改代码、改完还得重新理解上下文。

这里把它们集中成一个配置对象，默认值就是当前实际生效的数值——因此
未设置管理页覆盖值或环境变量时使用默认值。管理页保存值优先于环境变量，
无需修改代码或重启进程。

只暴露真正会影响检索效果的参数。内部实现细节（如分词正则）不在此列：
它们属于算法的一部分，不该随着部署环境漂移。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from backend.logging_conf import log_event
from backend.schemas.retrieval import RetrievalSettings


def _env_float(name: str, default: float, *, low: float, high: float) -> float:
    """读取浮点配置，越界或无法解析时退回默认值。

    配置错误的代价不该是「检索静默变差」，所以这里做区间校验：
    超出合理范围的取值一律忽略，并在默认值上继续工作。
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not (low <= value <= high):
        return default
    return value


def _env_int(name: str, default: int, *, low: int, high: int) -> int:
    """读取整数配置，越界或无法解析时退回默认值。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if not (low <= value <= high):
        return default
    return value


@dataclass
class RetrievalConfig:
    """一次检索使用的参数集合。"""

    # 候选预取：召回阶段多取一些，给去重与重排留空间
    prefetch_multiplier: int = 5
    prefetch_floor: int = 50

    # RRF 融合
    rrf_k: int = 60
    vector_weight: float = 0.7
    keyword_weight: float = 0.3

    # 重排
    max_rerank_candidates: int = 200
    threshold: float = 0.0
    degrade_floor: float = 0.3
    degrade_factor: float = 0.7
    fallback_min_score: float = 0.15

    # 复合分权重
    composite_rerank: float = 0.6
    composite_recall: float = 0.3
    composite_source: float = 0.1

    # 去冗余
    mmr_lambda: float = 0.7

    # 来源权重（按知识类型）
    source_weights: dict[str, float] = field(
        default_factory=lambda: {"rule": 1.0, "faq": 0.8, "manual": 0.6}
    )

    def prefetch_depth(self, top_k: int) -> int:
        """本题取多少候选：至少达到地板值，目标多时按倍数放大。"""
        return max(top_k * self.prefetch_multiplier, self.prefetch_floor)

    def to_dict(self) -> dict:
        """用于诊断输出，便于确认本次实际用的是哪套参数。"""
        return {
            "prefetch_depth_floor": self.prefetch_floor,
            "prefetch_multiplier": self.prefetch_multiplier,
            "rrf_k": self.rrf_k,
            "vector_weight": self.vector_weight,
            "keyword_weight": self.keyword_weight,
            "max_rerank_candidates": self.max_rerank_candidates,
            "threshold": self.threshold,
            "degrade_floor": self.degrade_floor,
            "degrade_factor": self.degrade_factor,
            "fallback_min_score": self.fallback_min_score,
            "mmr_lambda": self.mmr_lambda,
            "composite_rerank": self.composite_rerank,
            "composite_recall": self.composite_recall,
            "composite_source": self.composite_source,
            "source_weights": dict(self.source_weights),
        }


def load_retrieval_config() -> RetrievalConfig:
    """管理页保存值优先于环境变量；每次读取使共享文件的进程及时生效。"""
    config = RetrievalConfig(
        prefetch_multiplier=_env_int("RETRIEVAL_PREFETCH_MULTIPLIER", 5, low=1, high=20),
        prefetch_floor=_env_int("RETRIEVAL_PREFETCH_FLOOR", 50, low=1, high=1000),
        rrf_k=_env_int("RETRIEVAL_RRF_K", 60, low=1, high=1000),
        vector_weight=_env_float("RETRIEVAL_VECTOR_WEIGHT", 0.7, low=0.0, high=1.0),
        keyword_weight=_env_float("RETRIEVAL_KEYWORD_WEIGHT", 0.3, low=0.0, high=1.0),
        max_rerank_candidates=_env_int(
            "RETRIEVAL_MAX_RERANK_CANDIDATES", 200, low=1, high=2000
        ),
        threshold=_env_float("RETRIEVAL_THRESHOLD", 0.0, low=0.0, high=1.0),
        degrade_floor=_env_float("RETRIEVAL_DEGRADE_FLOOR", 0.3, low=0.0, high=1.0),
        degrade_factor=_env_float("RETRIEVAL_DEGRADE_FACTOR", 0.7, low=0.1, high=1.0),
        fallback_min_score=_env_float(
            "RETRIEVAL_FALLBACK_MIN_SCORE", 0.15, low=0.0, high=1.0
        ),
        composite_rerank=_env_float("RETRIEVAL_COMPOSITE_RERANK", 0.6, low=0.0, high=1.0),
        composite_recall=_env_float("RETRIEVAL_COMPOSITE_RECALL", 0.3, low=0.0, high=1.0),
        composite_source=_env_float("RETRIEVAL_COMPOSITE_SOURCE", 0.1, low=0.0, high=1.0),
        mmr_lambda=_env_float("RETRIEVAL_MMR_LAMBDA", 0.7, low=0.0, high=1.0),
    )
    if config.vector_weight + config.keyword_weight <= 0:
        config.vector_weight, config.keyword_weight = 0.7, 0.3
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
        saved = RetrievalSettings.model_validate(data)
    except FileNotFoundError:
        return config
    except (OSError, ValueError) as exc:
        log_event("retrieval.settings_read", error_type=type(exc).__name__)
        return config
    for name, value in saved.model_dump().items():
        setattr(config, name, value)
    return config


def _settings_path() -> Path:
    return Path(__file__).resolve().parent.parent / "retrieval_settings.json"


def current_settings() -> RetrievalSettings:
    config = load_retrieval_config()
    return RetrievalSettings(**{
        name: getattr(config, name) for name in RetrievalSettings.model_fields
    })


def save_settings(settings: RetrievalSettings) -> RetrievalSettings:
    """完整替换六项设置；原子换名避免读取半份 JSON，最后成功写入者生效。"""
    target = _settings_path()
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent,
        prefix=target.name + ".", suffix=".tmp", delete=False,
    ) as output:
        output.write(settings.model_dump_json(indent=2))
        output.flush()
        os.fsync(output.fileno())
        temporary = output.name
    os.replace(temporary, target)
    return settings
