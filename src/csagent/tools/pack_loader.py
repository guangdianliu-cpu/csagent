"""从 tool_packs.yaml 加载并实例化工具包（支持 profile overlay）。"""

from __future__ import annotations

import copy
import importlib
from typing import Any

import structlog
import yaml

from csagent.common.exceptions import ToolPackLoadError, ToolPackNotFoundError
from csagent.infra.config import CONFIGS_DIR
from csagent.tools.base import BaseTool, ToolPack

logger = structlog.get_logger(__name__)


def _import_class(dotted_path: str) -> type:
    """动态导入类：'csagent.tools.packs.script_pack.ScriptToolPack' → class。"""
    module_path, class_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base（override 优先）。"""
    result = copy.deepcopy(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


class ToolPackLoader:
    """配置驱动的工具包加载器。

    职责：
    1. 解析 tool_packs.yaml
    2. 应用 profile overlay 的 tool_pack_overrides（深合并）
    3. 按 pack_class 动态导入并实例化 ToolPack
    4. 缓存已加载的 ToolPack（同名 pack 只实例化一次）
    """

    def __init__(self) -> None:
        self._packs: dict[str, ToolPack] = {}
        self._pack_configs: dict[str, dict[str, Any]] = {}
        self._load_config()

    def _load_config(self) -> None:
        path = CONFIGS_DIR / "tool_packs.yaml"
        if not path.exists():
            logger.warning("tool_packs_yaml_not_found", path=str(path))
            return
        with path.open(encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        packs = raw.get("tool_packs") or {}
        if isinstance(packs, dict):
            self._pack_configs = packs

        # 应用 profile overlay 的 tool_pack_overrides
        try:
            from csagent.infra.agents_config import load_tool_pack_overrides
            overrides = load_tool_pack_overrides()
            for pack_name, override_cfg in overrides.items():
                if pack_name in self._pack_configs:
                    self._pack_configs[pack_name] = _deep_merge(
                        self._pack_configs[pack_name], override_cfg
                    )
                    logger.info("tool_pack_override_applied", pack=pack_name)
        except Exception:  # noqa: BLE001
            pass  # profile overlay 是可选的，加载失败不影响启动


    def load_tools(self, pack_names: list[str]) -> list[BaseTool]:
        """加载指定工具包，返回所有工具实例的扁平列表。"""
        all_tools: list[BaseTool] = []
        for name in pack_names:
            pack = self._get_or_create_pack(name)
            if pack is not None:
                all_tools.extend(pack.get_tools())
        return all_tools

    def get_pack(self, name: str) -> ToolPack | None:
        """返回已加载的工具包；未加载时尝试懒创建。"""
        return self._get_or_create_pack(name)

    def _get_or_create_pack(self, name: str) -> ToolPack | None:
        """获取或创建工具包（懒加载，同名只实例化一次）。"""
        if name in self._packs:
            return self._packs[name]

        cfg = self._pack_configs.get(name)
        if cfg is None:
            logger.warning("tool_pack_not_defined", pack=name)
            return None

        pack_class_path = str(cfg.get("pack_class") or "").strip()
        if not pack_class_path:
            logger.warning("tool_pack_no_class", pack=name)
            return None

        # Step 1: 动态导入 pack_class
        try:
            pack_class = _import_class(pack_class_path)
        except (ImportError, AttributeError, ValueError) as exc:
            logger.error("tool_pack_import_failed", pack=name, error=str(exc))
            raise ToolPackLoadError(f"无法导入工具包 {name}: {exc}") from exc

        # Step 2: 实例化并写入元数据
        pack: ToolPack = pack_class()
        pack.pack_name = name
        pack.description = str(cfg.get("description") or name)

        # Step 3: 合并 config；把顶层 tools 声明一并传入，供 pack 选用
        init_config = dict(cfg.get("config") or {})
        if "tools" in cfg and "tools" not in init_config:
            init_config["tools"] = cfg["tools"]

        # Step 4: initialize
        try:
            pack.initialize(init_config)
        except Exception as exc:  # noqa: BLE001
            logger.error("tool_pack_init_failed", pack=name, error=str(exc))
            raise ToolPackLoadError(f"工具包 {name} 初始化失败: {exc}") from exc

        self._packs[name] = pack
        logger.info(
            "tool_pack_loaded",
            pack=name,
            tools=[t.name for t in pack.get_tools()],
        )
        return pack

    async def prepare_packs(self, pack_names: list[str]) -> None:
        """预加载工具包并调用可选的 async connect（如 MCP 动态发现）。

        须在创建 ToolRegistry 之前调用，确保 MCP 等动态发现的工具已就绪。
        """
        seen: set[str] = set()
        for name in pack_names:
            if not name or name in seen:
                continue
            seen.add(name)
            pack = self._get_or_create_pack(name)
            if pack is None:
                continue
            connect = getattr(pack, "connect", None)
            if connect is None:
                continue
            try:
                await connect()
                logger.info(
                    "tool_pack_prepared",
                    pack=name,
                    tools=[t.name for t in pack.get_tools()],
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("tool_pack_prepare_failed", pack=name, error=str(exc))

    async def shutdown_all(self) -> None:
        """关闭所有已加载的工具包。"""
        for name, pack in list(self._packs.items()):
            try:
                await pack.shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.warning("tool_pack_shutdown_failed", pack=name, error=str(exc))

    def get_loaded_packs(self) -> dict[str, ToolPack]:
        return dict(self._packs)

    def require_pack(self, name: str) -> ToolPack:
        """严格获取工具包；不存在则抛 ToolPackNotFoundError。"""
        pack = self._get_or_create_pack(name)
        if pack is None:
            raise ToolPackNotFoundError(f"工具包未定义: {name}")
        return pack
