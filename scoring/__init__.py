"""离线判定与聚合（v4：评测结果管理改造，doc/09 方案 §11 包结构）。

分层：拆条与题目指纹（items）→ 机器算子链（operators/chain）→ 评测库
（store：SQLite，判定与人工数据的唯一事实来源）→ 有效结论（effective，
人工改判为覆盖层）→ 计分规则（rules）→ 视图与比较轴（views）→ CLI / 复核服务。
契约与口径见 doc/09-评测结果管理改造方案.md 与 doc/04 §6。
"""
__version__ = "4.0.0"
