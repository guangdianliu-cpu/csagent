"""语义查询通用引擎：Catalog（实体/字段目录）+ QuerySpec 校验，与业务领域无关。

真正发请求的 backend、以及各领域自己的 Catalog YAML，都不属于
这一层——按「基础工具是一等公民，业务逻辑在 Skill」的原则，它们跟着各自的 Skill走
（见对应的 Skill 脚本），本包不感知任何具体领域。
"""

from csagent.tools.semantic.executor import SemanticQueryExecutor
from csagent.tools.semantic.models import FilterSpec, OrderBySpec, QuerySpec

__all__ = [
    "FilterSpec",
    "OrderBySpec",
    "QuerySpec",
    "SemanticQueryExecutor",
]
