from __future__ import annotations

from main_logic.core.tool_calling import ToolCallingMixin
from main_logic.tool_calling import ToolRegistry


class _ToolManager(ToolCallingMixin):
    def __init__(self) -> None:
        self.user_language = "en"
        self.tool_registry = ToolRegistry()


def test_only_one_generic_public_knowledge_tool_is_registered(monkeypatch):
    monkeypatch.delenv("NEKO_DISABLE_BUILTIN_TOOLS", raising=False)
    manager = _ToolManager()

    manager._register_builtin_tools()

    public_tool = manager.tool_registry.get("query_public_knowledge")
    assert public_tool is not None
    assert manager.tool_registry.get("search_public_meme_knowledge") is None
    assert manager.tool_registry.get("search_moegirl_knowledge") is None
    assert public_tool.metadata["domain"] == "public_knowledge"
    assert "must call this tool with mode=sample" in public_tool.description
    assert public_tool.parameters["required"] == ["query"]
    assert public_tool.parameters["properties"]["collection"]["enum"] == [
        "all",
        "meme",
        "corpora",
    ]
    assert public_tool.parameters["properties"]["mode"]["enum"] == [
        "lookup",
        "sample",
    ]
    assert public_tool.parameters["properties"]["limit"]["maximum"] == 3
