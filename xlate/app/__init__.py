"""xlate 后端包。

分层：
    core      纯函数：归一化 / 六条不变量 / 领域归并
    models    Pydantic 契约（入口防线）
    glossary  词库存储：两级索引 + 原子落盘
    llm       LLM 适配层（Key 只在服务端）
    main      FastAPI 应用与路由
    views     页面渲染
"""
from .main import app

__all__ = ["app"]
