"""Preserve model argument identity before SDK coercion or ignored extra fields."""

import inspect
import re
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints

from strands.hooks import BeforeToolsEvent, HookProvider, HookRegistry

_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z", re.ASCII)
_REQUIRED = {"toolUseId", "name", "input"}
_ALLOWED = _REQUIRED | {"reasoningSignature"}


def _signature_valid(use):
    if "reasoningSignature" not in use:
        return True
    value = use["reasoningSignature"]
    try:
        return type(value) is str and len(value.encode("utf-8")) <= 65536
    except UnicodeError:
        return False


def _supported(annotation):
    if annotation in (str, int, type(None)):
        return True
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (UnionType, Union):
        return all(_supported(item) for item in args)
    return origin is list and args == (str,)


def _matches(value, annotation):
    if annotation in (str, int, type(None)):
        return type(value) is annotation
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (UnionType, Union):
        return any(_matches(value, item) for item in args)
    return type(value) is list and all(type(item) is str for item in value)


class FieldSDKRequestGuard(HookProvider):
    """Validate the complete assembled batch before any bound method can run."""

    def __init__(self, functions):
        self._arguments = {}
        self._seen = set()
        for function in functions:
            signature = inspect.signature(function)
            hints = get_type_hints(inspect.unwrap(function))
            arguments = {}
            for name, parameter in signature.parameters.items():
                annotation = hints.get(name)
                if (
                    parameter.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
                    or parameter.default is not inspect.Parameter.empty
                    or not _supported(annotation)
                ):
                    raise ValueError("unsupported field SDK argument contract")
                arguments[name] = annotation
            if function.__name__ in self._arguments:
                raise ValueError("duplicate field SDK tool registration")
            self._arguments[function.__name__] = arguments

    def register_hooks(self, registry: HookRegistry, **_kwargs) -> None:
        registry.add_callback(BeforeToolsEvent, self.before_tools)

    def before_tools(self, event: BeforeToolsEvent) -> None:
        uses = [block["toolUse"] for block in event.message["content"] if "toolUse" in block]
        ids = set()
        for use in uses:
            if (
                type(use) is not dict
                or not _REQUIRED <= set(use) <= _ALLOWED
                or not _signature_valid(use)
                or type(use["toolUseId"]) is not str
                or not _ID.fullmatch(use["toolUseId"])
                or use["toolUseId"] in ids
                or use["toolUseId"] in self._seen
            ):
                raise RuntimeError("Invalid field SDK tool request identity.")
            ids.add(use["toolUseId"])
        self._seen.update(ids)
        for use in uses:
            name, inputs = use["name"], use["input"]
            arguments = self._arguments.get(name) if type(name) is str else None
            if (
                arguments is None
                or type(inputs) is not dict
                or set(inputs) != set(arguments)
                or any(
                    not _matches(inputs[key], annotation) for key, annotation in arguments.items()
                )
            ):
                event.cancel = "Invalid field SDK tool arguments."
                return
