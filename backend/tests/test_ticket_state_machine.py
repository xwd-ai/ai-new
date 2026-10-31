"""工单状态机单元测试。

覆盖范围：
1. 状态枚举完整性 —— 六个状态值均已定义且唯一。
2. 合法流转表 —— 逐一验证 VALID_TRANSITIONS 中声明的每一条合法流转。
3. 非法流转 —— 遍历全部状态对，凡未声明的一律拒绝（含 closed 无出边）。
4. 校验函数 —— can_transition / validate_transition / is_valid_status 的行为。
5. 业务异常 —— 非法流转与未知状态均抛出 TicketTransitionError，信息明确。

测试仅依赖内存中的流转表与校验函数，不触及数据库。
"""

import itertools

import pytest

from backend.models.ticket import (
    VALID_TRANSITIONS,
    TicketErrorCode,
    TicketStatus,
    TicketTransitionError,
    can_transition,
    is_valid_status,
    validate_transition,
)


ALL_STATUSES = list(TicketStatus)


def test_状态枚举共六种且唯一():
    """状态枚举应恰好包含六种状态，值互不重复。"""
    assert len(ALL_STATUSES) == 6
    values = [s.value for s in ALL_STATUSES]
    assert len(set(values)) == 6


def test_流转表覆盖全部已知状态():
    """每个已知状态都应在流转表中有对应条目（closed 对应空集合）。"""
    assert set(VALID_TRANSITIONS.keys()) == set(ALL_STATUSES)


def test_所有合法流转均被允许():
    """流转表中声明的每一条合法流转，can_transition 与 validate_transition 都应通过。"""
    for current, targets in VALID_TRANSITIONS.items():
        for target in targets:
            assert can_transition(current, target) is True
            # 合法流转不应抛异常
            validate_transition(current, target)


def test_所有未声明流转均被拒绝():
    """任意状态对若不在流转表中声明，can_transition 应返回 False。"""
    for current in ALL_STATUSES:
        for target in ALL_STATUSES:
            if target not in VALID_TRANSITIONS[current]:
                assert can_transition(current, target) is False


def test_closed状态无出边():
    """closed 是终态，不能流转到任何其他状态（包括自身）。"""
    for target in ALL_STATUSES:
        assert can_transition(TicketStatus.CLOSED, target) is False


@pytest.mark.parametrize(
    "current,target",
    [
        # 典型非法流转组合
        (TicketStatus.OPEN, TicketStatus.OPEN),
        (TicketStatus.OPEN, TicketStatus.PENDING_CONFIRM),
        (TicketStatus.RESOLVED, TicketStatus.OPEN),
        (TicketStatus.RESOLVED, TicketStatus.IN_PROGRESS),
        (TicketStatus.RESOLVED, TicketStatus.TRANSFERRED_HUMAN),
        (TicketStatus.PENDING_CONFIRM, TicketStatus.IN_PROGRESS),
        (TicketStatus.PENDING_CONFIRM, TicketStatus.RESOLVED),
        (TicketStatus.PENDING_CONFIRM, TicketStatus.TRANSFERRED_HUMAN),
        (TicketStatus.TRANSFERRED_HUMAN, TicketStatus.OPEN),
        (TicketStatus.CLOSED, TicketStatus.OPEN),
    ],
)
def test_非法流转抛出业务异常(current, target):
    """非法流转应抛出 TicketTransitionError，且携带当前与目标状态。"""
    with pytest.raises(TicketTransitionError) as exc:
        validate_transition(current, target)
    assert exc.value.current == current.value
    assert exc.value.target == target.value
    assert current.value in exc.value.message
    assert target.value in exc.value.message


def test_未知当前状态被拒绝():
    """传入未知的当前状态字符串，应抛出「未知状态」错误。"""
    with pytest.raises(TicketTransitionError) as exc:
        validate_transition("ghost_status", TicketStatus.OPEN)
    assert "未知" in exc.value.message


def test_未知目标状态被拒绝():
    """传入未知的目标状态字符串，应抛出「未知状态」错误。"""
    with pytest.raises(TicketTransitionError) as exc:
        validate_transition(TicketStatus.OPEN, "ghost_status")
    assert "未知" in exc.value.message


def test_字符串状态等价于枚举状态():
    """字符串形式的合法状态值应能通过校验，并与枚举行为一致。"""
    # 字符串 -> 字符串
    validate_transition("open", "in_progress")
    # 字符串 -> 枚举
    validate_transition("in_progress", TicketStatus.RESOLVED)
    # 枚举 -> 字符串
    validate_transition(TicketStatus.RESOLVED, "closed")


def test_is_valid_status判定():
    """is_valid_status 应正确识别已知/未知状态字符串。"""
    assert is_valid_status("open") is True
    assert is_valid_status("closed") is True
    assert is_valid_status("transferred_human") is True
    assert is_valid_status("ghost") is False
    assert is_valid_status("") is False
    assert is_valid_status("OPEN") is False  # 大小写敏感


def test_枚举组合穷举与流转表一致():
    """can_transition 对全部 36 种状态对的判定应与流转表逐项一致。"""
    declared_pairs = {
        (c, t)
        for c, targets in VALID_TRANSITIONS.items()
        for t in targets
    }
    for current, target in itertools.product(ALL_STATUSES, repeat=2):
        expected = (current, target) in declared_pairs
        assert can_transition(current, target) is expected


def test_流转异常携带统一错误码与响应体():
    """非法流转异常可直接转成统一错误体：错误码 + 文案 + 原/目标状态。"""
    with pytest.raises(TicketTransitionError) as exc:
        validate_transition("closed", "open")

    assert exc.value.code == TicketErrorCode.INVALID_TRANSITION
    assert exc.value.to_payload() == {
        "error": "非法状态流转：closed -> open",
        "error_code": TicketErrorCode.INVALID_TRANSITION,
        "error_detail": {"from_status": "closed", "to_status": "open"},
    }


def test_未知状态使用独立错误码():
    """未知状态值不是流转非法，用独立错误码区分，便于调用方给出不同提示。"""
    with pytest.raises(TicketTransitionError) as exc:
        validate_transition("ghost_status", "open")

    assert exc.value.code == TicketErrorCode.UNKNOWN_STATUS
