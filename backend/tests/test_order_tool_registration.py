"""订单工具注册与调用约定测试。

覆盖四组：

- 注册对齐：注册项与工具定义同名同描述，只读标记与白名单一致；
- 身份绑定：登录身份在注册时定死，执行参数里的身份键被丢弃；
- 调用约定：未识别字段忽略、匿名执行报未登录、同名先注册者胜；
- 执行结果：走注册表执行与直调 run_order_tool 结论一致，可并发归批。

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
        f"sqlite:///{tmp_path / 'tool_registry.db'}",
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
    """插入一条订单，默认是 u-tool 名下服务中的专业版订单。"""
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


# ------------------------------ 注册对齐 ------------------------------


def test_registered_spec_matches_tool_definition():
    registry = tools.ToolRegistry()

    spec = orders.register_order_tool(registry, "u-tool")
    definition = orders.order_tool_definition()

    assert spec is registry.get(orders.ORDER_TOOL_NAME)
    assert spec.name == definition["name"] == orders.ORDER_TOOL_NAME
    assert spec.description == definition["description"] == orders.ORDER_TOOL_DESCRIPTION
    assert spec.read_only is True


def test_registered_tool_is_whitelisted_for_concurrency():
    registry = tools.ToolRegistry()
    spec = orders.register_order_tool(registry, "u-tool")

    assert spec.read_only is tools.can_run_concurrently(orders.ORDER_TOOL_NAME)
    assert tools.plan_execution([orders.ORDER_TOOL_NAME, "search_knowledge"]) == [
        [orders.ORDER_TOOL_NAME, "search_knowledge"]
    ]


def test_handler_factory_builds_callable():
    handler = orders.make_order_tool_handler("u-tool")

    assert callable(handler)


# ------------------------------ 身份绑定 ------------------------------


def test_handler_uses_bound_identity_not_arguments(factory):
    """参数里夹带 user_id 也不生效，查到的仍是绑定用户的订单。"""
    _add_order(factory, id="O-9001", user_id="u-tool")
    _add_order(factory, id="O-9002", user_id="u-other")

    handler = orders.make_order_tool_handler("u-tool")
    result = handler(user_id="u-other", user="u-other")

    assert result.success is True
    assert result.data["order"]["primary_order_id"] == "O-9001"


def test_registry_execute_drops_identity_keys(factory):
    """走注册表执行同样丢弃身份键，且不会因参数重复报错。"""
    _add_order(factory, id="O-9001", user_id="u-tool")
    _add_order(factory, id="O-9002", user_id="u-other")
    registry = tools.ToolRegistry()
    orders.register_order_tool(registry, "u-tool")

    result = registry.execute(orders.ORDER_TOOL_NAME, user_id="u-other", user="u-other")

    assert result.success is True
    assert result.data["order"]["primary_order_id"] == "O-9001"


# ------------------------------ 调用约定 ------------------------------


def test_unknown_arguments_are_ignored_via_registry(factory):
    _add_order(factory)
    registry = tools.ToolRegistry()
    orders.register_order_tool(registry, "u-tool")

    result = registry.execute(orders.ORDER_TOOL_NAME, limit=10, verbose=True)

    assert result.success is True
    assert result.data["mode"] == "list"


def test_anonymous_registration_reports_no_user(factory):
    registry = tools.ToolRegistry()
    orders.register_order_tool(registry, orders.ORDER_TOOL_ANONYMOUS)

    result = registry.execute(orders.ORDER_TOOL_NAME)

    assert result.success is False
    assert result.error_code == orders.ORDER_TOOL_ERROR_NO_USER
    assert "未登录" in result.error


def test_first_registration_wins_for_same_name(factory):
    """同名先注册者胜，后一次注册不覆盖既有执行体。"""
    _add_order(factory, id="O-9001", user_id="u-tool")
    _add_order(factory, id="O-9002", user_id="u-other")
    registry = tools.ToolRegistry()

    first = orders.register_order_tool(registry, "u-tool")
    second = orders.register_order_tool(registry, "u-other")

    assert second is first
    result = registry.execute(orders.ORDER_TOOL_NAME)
    assert result.data["order"]["primary_order_id"] == "O-9001"


# ------------------------------ 执行结果 ------------------------------


def test_registry_execution_matches_direct_run(factory):
    """注册表只是入口，结论口径与直调完全一致。"""
    _add_order(factory)
    registry = tools.ToolRegistry()
    orders.register_order_tool(registry, "u-tool")

    via_registry = registry.execute(orders.ORDER_TOOL_NAME, feature="批量导出")
    direct = orders.run_order_tool("u-tool", feature="批量导出")

    assert via_registry.success is direct.success is True
    assert via_registry.data == direct.data
    assert via_registry.output == direct.output


def test_result_data_is_json_serializable_via_registry(factory):
    _add_order(factory)
    registry = tools.ToolRegistry()
    orders.register_order_tool(registry, "u-tool")

    result = registry.execute(orders.ORDER_TOOL_NAME)

    assert result.success is True
    assert json.loads(json.dumps(result.data, ensure_ascii=False))["tool"] == orders.ORDER_TOOL_NAME
