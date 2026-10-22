"""离线判定与聚合（v3：判定算子扩展，方案 v2 §11 包结构）。

分层：拆条（items）→ 机器算子链（operators/chain）→ 有效结论（effective，
人工改判为覆盖层）→ 口径（policy）→ 视图与对比（views）→ CLI / 复核服务。
契约与口径见 doc/《判定算子扩展方案》与 doc/04 §6。
"""
__version__ = "3.0.0"
