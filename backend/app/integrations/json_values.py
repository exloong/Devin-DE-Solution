"""Explicit recursive JSON types used instead of ``typing.Any``.

Every integration boundary parses untrusted JSON. Describing that JSON with
recursive aliases keeps the parsing code honest: a value has to be narrowed by
one of the helpers in :mod:`app.integrations.transport` before it can be used
as a string, integer, object, or array.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypeAlias

JsonValue: TypeAlias = (
    None | bool | int | float | str | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
)
JsonObject: TypeAlias = Mapping[str, JsonValue]
JsonArray: TypeAlias = Sequence[JsonValue]

__all__ = ["JsonArray", "JsonObject", "JsonValue"]
