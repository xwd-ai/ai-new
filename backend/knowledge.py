# 知识库（demo 阶段内置静态知识，不引向量数据库）
# 知识条目：产品手册 / FAQ / 服务规则
# 检索用简单关键词匹配（后续可升级为向量检索 + 混合检索 + rerank）

KNOWLEDGE = [
    {
        "id": "k-export",
        "kind": "manual",  # 产品手册
        "title": "产品手册《导出功能排查指南》",
        "keywords": ["导出", "失败", "export", "报错", "无法导出"],
        "content": (
            "导出功能常见失败原因与处理：\n"
            "1. 导出目录无写入权限 —— 检查目标文件夹是否只读，换一个有写权限的目录。\n"
            "2. 磁盘空间不足 —— 清理磁盘后重试。\n"
            "3. 客户端版本过旧 —— 升级到最新专业版客户端。\n"
            "4. 杀毒软件拦截 —— 将软件加入白名单后重试。\n"
            "5. 数据量过大 —— 专业版支持批量导出，基础版仅支持单文件导出。"
        ),
    },
    {
        "id": "k-activate",
        "kind": "faq",
        "title": "FAQ《激活码常见问题》",
        "keywords": ["激活", "激活码", "无效", "激活失败"],
        "content": (
            "激活码提示无效的处理：\n"
            "1. 确认激活码与购买版本一致（专业版/基础版激活码不通用）。\n"
            "2. 核对激活码大小写与连字符。\n"
            "3. 确认激活码未过期、未在其它设备使用超过授权数量。"
        ),
    },
    {
        "id": "k-refund",
        "kind": "faq",
        "title": "FAQ《退款申请流程》",
        "keywords": ["退款", "退费", "申请退款"],
        "content": (
            "退款申请流程：\n"
            "1. 购买 7 天内且未超过试用额度，可申请全额退款。\n"
            "2. 在「我的订单」中点击申请退款，填写原因。\n"
            "3. 客服审核通过后 3-5 个工作日原路退回。"
        ),
    },
    {
        "id": "k-service-rule",
        "kind": "rule",  # 服务规则
        "title": "服务规则《售后支持范围》",
        "keywords": ["服务范围", "售后", "支持", "转人工", "专业版"],
        "content": (
            "售后支持范围：\n"
            "1. 专业版用户享受优先客服支持（服务期内）。\n"
            "2. 基础版用户仅提供社区支持。\n"
            "3. 服务期外的用户需续费后才能获得人工支持。\n"
            "4. 超出服务范围或多次排查无效的问题，转人工处理。"
        ),
    },
]


def search_knowledge(query: str, top_k: int = 3) -> list[dict]:
    """简单关键词匹配检索：命中知识条目关键词即返回，带来源标识。"""
    results = []
    q = query.lower()
    for item in KNOWLEDGE:
        if any(k.lower() in q or q in k.lower() for k in item["keywords"]):
            results.append(item)
    # 按匹配度排序：命中关键词越多越靠前
    def score(item):
        return sum(1 for k in item["keywords"] if k.lower() in q)
    results.sort(key=score, reverse=True)
    return results[:top_k]


def get_knowledge(knowledge_id: str) -> dict | None:
    for item in KNOWLEDGE:
        if item["id"] == knowledge_id:
            return item
    return None
