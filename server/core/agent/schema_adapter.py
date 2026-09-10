"""Provider-specific wire schemas; domain models and local validation stay unchanged."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


SCHEMA_ADAPTER_VERSION = 1


def adapt_schema(schema: dict[str, Any], provider: str) -> dict[str, Any]:
    if provider != "deepseek":
        return schema

    def visit(node: Any, refs: tuple[str, ...] = (), branch: bool = False) -> Any:
        if not isinstance(node, dict):
            return deepcopy(node)
        ref = node.get("$ref")
        if branch and isinstance(ref, str) and ref.startswith("#/"):
            if ref in refs:
                raise ValueError("DeepSeek anyOf schema contains a recursive local reference")
            target = schema
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            # Preserve sibling constraints as an intersection, rather than overwriting them.
            expanded = visit(target, (*refs, ref), branch=True)
            siblings = {key: value for key, value in node.items() if key != "$ref"}
            return {"allOf": [expanded, visit(siblings, refs)]} if siblings else expanded
        result = {}
        for key, value in node.items():
            if key in {"minLength", "maxLength", "minItems", "maxItems"}:
                continue
            if key in {"properties", "$defs", "definitions", "patternProperties"}:
                result[key] = {name: visit(item, refs) for name, item in value.items()}
            elif key in {"anyOf", "oneOf", "allOf", "prefixItems"}:
                result[key] = [visit(item, refs, branch=key == "anyOf") for item in value]
            elif key in {"items", "additionalProperties", "not", "if", "then", "else"}:
                result[key] = visit(value, refs)
            else:
                result[key] = deepcopy(value)
        return result

    return visit(schema)
