"""Admit complete assembled SDK batches before name or argument validation."""

from strands.hooks import BeforeToolsEvent, HookProvider, HookRegistry


class FieldSDKToolBudget(HookProvider):
    """Count admitted assembled requests; a refused overflow batch charges nothing."""

    def __init__(self):
        self._attempts = 0
        self._exhausted = False

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    def register_hooks(self, registry: HookRegistry, **_kwargs) -> None:
        registry.add_callback(BeforeToolsEvent, self.before_tools)

    def before_tools(self, event: BeforeToolsEvent) -> None:
        requested = sum(
            type(block) is dict and "toolUse" in block for block in event.message["content"]
        )
        if self._exhausted or requested > 16 - self._attempts:
            self._exhausted = True
            raise RuntimeError("Field SDK tool request limit exceeded.")
        self._attempts += requested
