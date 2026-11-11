"""订单查询只读工具测试。

覆盖四组：

- 定义：工具名、说明、参数 schema 与只读标记，以及定义与工具白名单的一致性；
- 入参规整：空白归一、超长拒绝、未识别参数忽略、未登录拒绝；
- 分派：按入参走功能判定 / 单订单判定 / 多订单视图三条路，mode 如实回显；
- 结果形态：查不到订单不是失败，data 可直接序列化给调用方。

时间一律由用例用相对当前时间构造，不依赖固定日期。
"""

import json
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import Order, Product
from backend.services import orders, tools

DAY = 86400


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    """临时库 + 两个产品的配置行，服务层 SessionLocal 指向它。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tool.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(orders, "SessionLocal", session_factory)

    db = session_factory()
    try:
        db.add_all(
            [
                Product(id="basic", name="基础版", features=["基础导出", "单文件上传", "社区支持"]),
                Product(
                    id="pro",
                    name="专业版",
                    features=["批量导出", "高级报表", "优先客服支持", "API 接口"],
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    yield session_factory
    engine.dispose()


def _add_order(factory, **overrides) -> None:
    """插入一条订单，默认是服务中的专业版订单。"""
    now = time.time()
    payload = {
        "id": "O-9001",
        "user_id": "u-tool",
        "product_tier": "pro",
        "status": "active",
        "started_at": now - 10 * DAY,
        "expires_at": now + 100 * DAY,
    }
    payload.update(overrides)
    db = factory()
    try:
        db.add(Order(**payload))
        db.commit()
    finally:
        db.close()


# ------------------------------ 定义 ------------------------------


def test_definition_carries_name_and_schema():
    definition = orders.order_tool_definition()

    assert definition["name"] == orders.ORDER_TOOL_NAME == "query_order"
    assert definition["read_only"] is True
    assert definition["parameters"]["type"] == "object"
    assert definition["parameters"]["required"] == []
    assert definition["parameters"]["additionalProperties"] is False
    assert set(definition["parameters"]["properties"]) == {"order_id", "feature"}
    assert definition["description"].strip()


def test_definition_does_not_expose_user_identity():
    """用户身份不能出现在参数里，否则模型可以指定查谁的订单。"""
    properties = orders.order_tool_definition()["parameters"]["properties"]

    assert "user_id" not in properties
    assert "user" not in properties


def test_definition_is_a_copy():
    """调用方改写返回值不能污染模块常量。"""
    definition = orders.order_tool_definition()
    definition["parameters"]["properties"].clear()

    assert "order_id" in orders.ORDER_TOOL_PARAMETERS["properties"]


def test_tool_name_matches_read_only_whitelist():
    """工具名与白名单一致，查询才能与其它只读工具并发。"""
    assert orders.ORDER_TOOL_NAME in tools.READ_ONLY_TOOLS
    assert orders.ORDER_TOOL_CONCURRENT is True
    assert tools.can_run_concurrently(orders.ORDER_TOOL_NAME) is True


# ------------------------------ 入参规整 ------------------------------


@pytest.mark.parametrize("user_id", ["", "   ", None, orders.ORDER_TOOL_ANONYMOUS, " Anonymous "])
def test_anonymous_user_is_rejected(user_id, factory):
    result = orders.run_order_tool(user_id)

    assert result.success is False
    assert result.error_code == orders.ORDER_TOOL_ERROR_NO_USER
    assert "未登录" in result.error
    assert result.data == {}


def test_long_feature_is_rejected(factory):
    _add_order(factory)

    result = orders.run_order_tool(
        "u-tool", feature="导" * (orders.ORDER_TOOL_FEATURE_MAX + 1)
    )

    assert result.success is False
    assert result.error_code == orders.ORDER_TOOL_ERROR_INVALID
    assert str(orders.ORDER_TOOL_FEATURE_MAX) in result.error


def test_long_order_id_is_rejected(factory):
    _add_order(factory)

    result = orders.run_order_tool(
        "u-tool", order_id="O" * (orders.ORDER_TOOL_ID_MAX + 1)
    )

    assert result.success is False
    assert result.error_code == orders.ORDER_TOOL_ERROR_INVALID
    assert str(orders.ORDER_TOOL_ID_MAX) in result.error


def test_feature_at_limit_is_accepted(factory):
    """刚好等于上限的入参不算超长，交给判定层给出结论。"""
    _add_order(factory)

    result = orders.run_order_tool(
        "u-tool", feature="导" * orders.ORDER_TOOL_FEATURE_MAX
    )

    assert result.success is True
    assert result.data["mode"] == "feature"


def test_unknown_parameters_are_ignored(factory):
    """工具协议演进时多传字段不该报错。"""
    _add_order(factory)

    result = orders.run_order_tool("u-tool", limit=10, verbose=True, user="u-other")

    assert result.success is True
    assert result.data["mode"] == "list"
    assert result.data["order"]["primary_order_id"] == "O-9001"


# ------------------------------ 分派 ------------------------------


def test_list_mode_without_arguments(factory):
    _add_order(factory)

    result = orders.run_order_tool("u-tool", order_id="   ", feature="  ")

    assert result.success is True
    assert result.data["mode"] == "list"
    assert result.data["order"]["has_order"] is True
    assert result.data["order"]["primary_order_id"] == "O-9001"
    assert result.output == result.data["order"]["message"]


def test_order_mode_with_order_id(factory):
    _add_order(factory)
    _add_order(factory, id="O-9002", product_tier="basic", expires_at=time.time() + 10 * DAY)

    result = orders.run_order_tool("u-tool", order_id="  O-9002 ")

    assert result.success is True
    assert result.data["mode"] == "order"
    assert result.data["order"]["order_id"] == "O-9002"
    assert result.data["order"]["tier"] == "basic"


def test_order_mode_rejects_foreign_order(factory):
    """别人的订单号按查不到处理，不暴露是否存在。"""
    _add_order(factory, user_id="u-other", id="O-9003")

    result = orders.run_order_tool("u-tool", order_id="O-9003")

    assert result.success is True
    assert result.data["mode"] == "order"
    assert result.data["order"]["has_order"] is False
    assert result.data["order"]["reason_code"] == "no_order"


def test_feature_mode_covers_all_orders(factory):
    """主订单是基础版时，另一条在期专业版覆盖的功能仍判为在范围内。"""
    _add_order(factory, id="O-9004", product_tier="basic", expires_at=time.time() + 20 * DAY)
    _add_order(factory, id="O-9005", product_tier="pro", expires_at=time.time() + 200 * DAY)

    result = orders.run_order_tool("u-tool", feature="批量导出")

    assert result.success is True
    assert result.data["mode"] == "feature"
    payload = result.data["order"]
    assert payload["in_scope"] is True
    assert payload["covered_by"] == "O-9005"
    assert payload["feature"]["name"] == "批量导出"


def test_feature_mode_reports_not_included(factory):
    _add_order(factory, product_tier="basic")

    result = orders.run_order_tool("u-tool", feature="批量导出")

    assert result.success is True
    assert result.data["order"]["in_scope"] is False
    assert result.data["order"]["reason_code"] == "feature_not_included"
    assert "批量导出" in result.output


def test_feature_takes_precedence_over_order_id(factory):
    """同时给出功能名与订单号时按功能判定，避免给出只看一条订单的片面结论。"""
    _add_order(factory)

    result = orders.run_order_tool("u-tool", feature="导出", order_id="O-9001")

    assert result.data["mode"] == "feature"


def test_user_without_order_is_not_a_failure(factory):
    result = orders.run_order_tool("u-nobody")

    assert result.success is True
    assert result.data["order"]["has_order"] is False
    assert result.output == "未查询到该用户的订单记录"


def test_expired_order_still_returns_data(factory):
    """订单已到期也要把结论带回去，模型据此提示续费。"""
    now = time.time()
    _add_order(
        factory,
        started_at=now - 100 * DAY,
        expires_at=now - 10 * DAY,
    )

    result = orders.run_order_tool("u-tool", feature="批量导出")

    assert result.success is True
    assert result.data["order"]["in_scope"] is False
    assert result.data["order"]["reason_code"] == "expired"


# ------------------------------ 结果形态 ------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"order_id": "O-9001"}, {"feature": "导出"}, {"feature": "不存在的能力"}],
)
def test_result_data_is_json_serializable(factory, kwargs):
    """结果要能直接进事件流与日志，不能夹带不可序列化的对象。"""
    _add_order(factory)

    result = orders.run_order_tool("u-tool", **kwargs)

    assert result.success is True
    assert json.loads(json.dumps(result.data, ensure_ascii=False))["tool"] == "query_order"


def test_output_is_single_line_and_non_empty(factory):
    _add_order(factory)

    result = orders.run_order_tool("u-tool")

    assert result.output
    assert "\n" not in result.output
    assert result.error == ""
    assert result.error_code == ""
