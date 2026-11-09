"""本轮的委托范围：加载的 Skill 把工具集与可读资源收窄到哪里。

Skill 的 ``allowed-tools`` 在 ``skills.config.enforce_allowed_tools: true`` 时是硬约束。
以前白名单直接 ``ContextVar.set`` 一个新 set：load_skill 跑在 ``asyncio.gather`` 的子任务里，
子任务里 set 的值不会回传，下一批工具调用根本看不到——硬约束从来没生效过（已实测）。

现在 Agent 在每轮开始时放一个**可变**的 :class:`DelegationScope`，子任务修改的是同一个
对象。范围只能逐级收窄：多个 Skill 的 allowed-tools 取并集（不把自己锁死），
资源访问限于本轮已加载的 Skill。被 registry 与 skill_paths 共用，单独成模块免得互相 import。
"""

from __future__ import annotations

from contextvars import ContextVar, Token


class DelegationScope:
    """本轮委托范围（可变，跨工具批次共享）。"""

    def __init__(self) -> None:
        # None = 没有 Skill 限定工具集；set = 只允许这些工具
        self.allowed_tools: set[str] | None = None
        # 硬约束模式下本轮加载过的 Skill；资源路径 <skill:name>/... 只认这些
        self.loaded_skills: set[str] = set()
        self.enforced = False


delegation_scope_var: ContextVar[DelegationScope | None] = ContextVar(
    "delegation_scope", default=None
)


def begin_delegation_scope() -> Token:
    """每轮开始时调用：放一个新的委托范围，返回 token 供本轮结束时 reset。"""
    return delegation_scope_var.set(DelegationScope())


def current_delegation() -> DelegationScope | None:
    return delegation_scope_var.get(None)


def _scope_for_update() -> DelegationScope:
    scope = delegation_scope_var.get(None)
    if scope is None:
        # 没有经 Agent 起头（单测直接调工具）：在当前上下文建一个，效果仅限当前上下文
        scope = DelegationScope()
        delegation_scope_var.set(scope)
    return scope


def restrict_tools(allowed_tools: list[str]) -> None:
    """把 Skill 的 allowed-tools 并入本轮白名单。"""
    scope = _scope_for_update()
    if scope.allowed_tools is None:
        scope.allowed_tools = set(allowed_tools)
    else:
        scope.allowed_tools.update(allowed_tools)


def note_skill_loaded(name: str) -> None:
    """硬约束模式下登记本轮加载的 Skill，之后只能读写它们的资源。"""
    scope = _scope_for_update()
    scope.enforced = True
    scope.loaded_skills.add(name)
