"""工具系统。

把知识检索、订单查询这类能力注册成「工具」，让执行循环按名字调用，
而不是在业务代码里硬编码调用顺序。

两条调度规则决定了工具怎么被执行：

- **只读工具可并发**：查订单、查知识库互不影响，同时发起能显著缩短
  等待时间。哪些是只读由显式白名单决定，不靠命名猜测——猜错一次的
  代价是数据被并发写坏。
- **写操作形成屏障**：写操作前后必须把在途的只读调用排空，保证
  「读到的是写之前或写之后的一致状态」，而不是读到写了一半的中间态。

未知工具一律按写操作处理：不认识的东西就当它可能有副作用，
宁可慢一点，也不要因为误判让它和别的调用并发。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# 只读工具白名单：列在这里的工具声明自身没有副作用，可以安全并发。
# 新增工具时默认不在名单内——想并发必须显式加进来。
READ_ONLY_TOOLS = frozenset(
    {
        "search_knowledge",
        "query_order",
        "get_source_detail",
    }
)


def can_run_concurrently(tool_name: str) -> bool:
    """该工具是否可以与其它只读工具并发执行。

    只有白名单内的工具返回 True；未知工具保守地返回 False。
    """
    return tool_name in READ_ONLY_TOOLS


@dataclass
class ToolResult:
    """一次工具执行的结果。"""

    success: bool
    output: str = ""
    error: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class ToolSpec:
    """一个已注册工具的定义。"""

    name: str
    description: str
    handler: Callable[..., ToolResult]
    read_only: bool = False

    def __post_init__(self) -> None:
        # 以白名单为准，避免注册时误标
        self.read_only = can_run_concurrently(self.name)


class ToolRegistry:
    """工具注册表。

    同名工具只保留第一次注册的定义：后注册的同名工具无法覆盖已有实现，
    避免意外覆盖掉内置工具的行为。
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        description: str,
        handler: Callable[..., ToolResult],
    ) -> None:
        if not name:
            raise ValueError("工具名不能为空")
        if name in self._tools:
            return
        self._tools[name] = ToolSpec(
            name=name,
            description=description,
            handler=handler,
            read_only=can_run_concurrently(name),
        )

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def read_only_names(self) -> list[str]:
        return sorted(n for n, t in self._tools.items() if t.read_only)

    def execute(self, name: str, **kwargs) -> ToolResult:
        """执行一个工具，把异常收敛成失败结果。

        工具内部抛错不应该让整个对话崩掉——失败本身也是一种观察结果，
        循环可以据此换一条路继续。
        """
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(success=False, error=f"工具不存在: {name}")
        try:
            return spec.handler(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 工具异常转成可观测结果
            return ToolResult(success=False, error=f"工具 {name} 执行失败: {exc}")

    def __len__(self) -> int:
        return len(self._tools)


def plan_execution(tool_names: list[str]) -> list[list[str]]:
    """把一批工具调用按并发/屏障规则切成若干执行批次。

    返回的是「批次列表」：同一批次内的工具可以并发执行，批次之间必须
    串行。遇到写操作时，它自己独占一个批次，前后的只读调用各自成组。

    这样调用方只需要按批次顺序执行，批内并发、批间串行，
    不必在每个调用点重复判断读写语义。
    """
    batches: list[list[str]] = []
    pending_reads: list[str] = []

    for name in tool_names:
        if can_run_concurrently(name):
            pending_reads.append(name)
            continue
        # 写操作：先把攒下的读操作排空，再让写操作独占一批
        if pending_reads:
            batches.append(pending_reads)
            pending_reads = []
        batches.append([name])

    if pending_reads:
        batches.append(pending_reads)
    return batches
