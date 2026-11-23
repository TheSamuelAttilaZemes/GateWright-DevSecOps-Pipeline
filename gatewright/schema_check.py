# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""A small JSON-Schema validator for the subset Gatewright's config uses.

Gatewright used to depend on `jsonschema`, which pulls in a Rust extension
(rpds-py). On platforms without a prebuilt wheel that meant installing a Rust
compiler just to install Gatewright. This pure-Python validator keeps Gatewright
installable on any Linux distribution/architecture.

Supported keywords: type, properties, required, additionalProperties, enum,
pattern, minLength, minimum, maximum, exclusiveMinimum, items, minItems, oneOf.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


@dataclass
class SchemaError:
    """One validation error; same attributes as jsonschema's ValidationError that Gatewright uses."""
    message: str
    validator: str
    validator_value: Any
    instance: Any
    absolute_path: list = field(default_factory=list)


def _type_ok(value, t) -> bool:
    types = t if isinstance(t, list) else [t]
    return any(_TYPES[x](value) for x in types)


def iter_errors(instance, schema: dict, path: list | None = None) -> Iterator[SchemaError]:
    """Yield every validation error of `instance` against `schema`."""
    path = path or []

    def err(msg, kw):
        return SchemaError(msg, kw, schema.get(kw), instance, list(path))

    if "oneOf" in schema:
        matches = [s for s in schema["oneOf"] if not any(True for _ in iter_errors(instance, s, path))]
        if len(matches) != 1:
            yield err(f"{instance!r} is not valid under any of the given schemas" if not matches
                      else f"{instance!r} is valid under more than one of the given schemas", "oneOf")
        return

    if "type" in schema and not _type_ok(instance, schema["type"]):
        t = schema["type"]
        wanted = repr(t) if isinstance(t, str) else " or ".join(map(repr, t))
        yield err(f"{instance!r} is not of type {wanted}", "type")
        return

    if "enum" in schema and instance not in schema["enum"]:
        yield err(f"{instance!r} is not one of {schema['enum']!r}", "enum")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            yield err(f"{instance!r} is too short", "minLength")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            yield err(f"{instance!r} does not match {schema['pattern']!r}", "pattern")

    if _TYPES["number"](instance):
        if "minimum" in schema and instance < schema["minimum"]:
            yield err(f"{instance!r} is less than the minimum of {schema['minimum']!r}", "minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            yield err(f"{instance!r} is greater than the maximum of {schema['maximum']!r}", "maximum")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            yield err(f"{instance!r} must be greater than {schema['exclusiveMinimum']!r}", "exclusiveMinimum")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            yield err(f"{instance!r} should have at least {schema['minItems']} item(s)", "minItems")
        if "items" in schema:
            for i, item in enumerate(instance):
                yield from iter_errors(item, schema["items"], path + [i])

    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in instance:
                yield err(f"{req!r} is a required property", "required")
        extra = schema.get("additionalProperties", True)
        unexpected = [k for k in instance if k not in props]
        if extra is False and unexpected:
            names = ", ".join(repr(k) for k in unexpected)
            verb = "was" if len(unexpected) == 1 else "were"
            yield err(f"Additional properties are not allowed ({names} {verb} unexpected)", "additionalProperties")
        for k, v in instance.items():
            if k in props:
                yield from iter_errors(v, props[k], path + [k])
            elif isinstance(extra, dict):
                yield from iter_errors(v, extra, path + [k])
