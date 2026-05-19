# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Selector DSL for hierarchical device + function discovery.

This module parses selector expressions used by the discovery, invocation, and
subscription APIs into a structured form that can be matched against device,
function, and event records.

Placement note: this module is dependency-free (stdlib only) and is consumed
by callers outside this package -- notably the discovery tools in
``device_connect_agent_tools``. It lives here as the lowest common ancestor
in the package dependency graph, not as edge-runtime code; ``DeviceRuntime``
and the driver framework do not import it.

Grammar overview:

    device(<filters>)                      # filter on device labels
    device(<filters>).function(<filters>)  # functions on a device subset (RPCs)
    device(<filters>).event(<filters>)     # events on a device subset
    function(<filters>)                    # all RPCs across the fleet
    event(<filters>)                       # all events across the fleet

Inside ``(...)``:

    key:value           single value match
    key:[v1,v2]         OR within a key (matches if label contains any value)
    key:pattern*        glob (``*``, ``?``)
    k1:v1,k2:v2         AND across keys
    bare-string         id/name match: ``device(robot-001)``
    *                   match all
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

# A label value is either a single string or a list of strings (composite identity).
LabelValue = Union[str, List[str]]
Labels = Dict[str, LabelValue]

# Characters that make ``fnmatch`` treat a pattern as a glob rather than a
# literal. Must stay in sync with ``fnmatch``'s grammar — ``*`` (any run),
# ``?`` (any single char), and ``[seq]`` / ``[!seq]`` character classes.
_GLOB_META = frozenset("*?[")


def _is_glob(pattern: str) -> bool:
    """True if ``pattern`` contains any ``fnmatch`` meta-character."""
    return any(c in _GLOB_META for c in pattern)


class SelectorParseError(ValueError):
    """Raised when a selector string cannot be parsed."""

    def __init__(self, message: str, source: str = "", position: Optional[int] = None):
        if position is not None and source:
            caret = " " * position + "^"
            full = f"{message} at position {position}\n  {source}\n  {caret}"
        elif source:
            full = f"{message}: {source!r}"
        else:
            full = message
        super().__init__(full)
        self.source = source
        self.position = position


class Scope(str, Enum):
    """Which entities a selector matches.

    DEVICE_ONLY     - device(...)
    DEVICE_FUNCTION - device(...).function(...)
    DEVICE_EVENT    - device(...).event(...)
    FUNCTION_ONLY   - function(...)
    EVENT_ONLY      - event(...)
    """
    DEVICE_ONLY = "device_only"
    DEVICE_FUNCTION = "device_function"
    DEVICE_EVENT = "device_event"
    FUNCTION_ONLY = "function_only"
    EVENT_ONLY = "event_only"


@dataclass(frozen=True)
class KeyFilter:
    """Filter on a single label key.

    Values are OR'd: any matching value is sufficient. Each value may contain
    glob characters per ``fnmatch`` semantics (``*``, ``?``, and ``[seq]`` /
    ``[!seq]`` character classes).

    ``children`` is reserved for grammar extensions (nested boolean
    expressions, AND-within-key, negation) and is empty in the current
    parser. Carrying the field on the dataclass now lets future versions
    populate it without breaking the public type shape.
    """
    key: str
    values: Tuple[str, ...]
    children: Tuple["KeyFilter", ...] = field(default_factory=tuple)

    def matches(self, label_value: Optional[LabelValue]) -> bool:
        """True iff the label value satisfies this key filter.

        For multi-valued labels (list), passes if any element matches any of
        this filter's values.
        """
        if label_value is None:
            return False
        actual: Tuple[str, ...]
        if isinstance(label_value, list):
            actual = tuple(label_value)
        else:
            actual = (label_value,)
        for pattern in self.values:
            if _is_glob(pattern):
                for a in actual:
                    if fnmatch.fnmatchcase(a, pattern):
                        return True
            else:
                if pattern in actual:
                    return True
        return False


@dataclass(frozen=True)
class Filter:
    """One axis of a selector - matches a single entity (device, function, or event).

    Combines an optional bare-string name match with AND-across-keys label
    filters. An empty Filter (no name match, no key filters) matches every
    entity, so ``*`` and empty parens both reduce to that case.
    """
    name_match: Optional[str] = None
    key_filters: Tuple[KeyFilter, ...] = field(default_factory=tuple)

    def matches(self, name: str, labels: Optional[Labels]) -> bool:
        """True iff this filter matches the given entity."""
        if self.name_match is not None:
            pattern = self.name_match
            if _is_glob(pattern):
                if not fnmatch.fnmatchcase(name, pattern):
                    return False
            elif name != pattern:
                return False
        for kf in self.key_filters:
            label_value = labels.get(kf.key) if labels else None
            if not kf.matches(label_value):
                return False
        return True


@dataclass(frozen=True)
class Selector:
    """Parsed selector expression.

    Each axis is an optional :class:`Filter`. A ``None`` axis is vacuously
    True - ``matches_function`` on a device-only selector returns True so the
    caller can write a single-pass enumeration without scope branching.
    """
    scope: Scope
    device: Optional[Filter] = None
    function: Optional[Filter] = None
    event: Optional[Filter] = None
    raw: str = ""

    def matches_device(self, name: str, labels: Optional[Labels]) -> bool:
        if self.device is None:
            return True
        return self.device.matches(name, labels)

    def matches_function(self, name: str, labels: Optional[Labels]) -> bool:
        if self.function is None:
            return True
        return self.function.matches(name, labels)

    def matches_event(self, name: str, labels: Optional[Labels]) -> bool:
        if self.event is None:
            return True
        return self.event.matches(name, labels)


# -- Parsing -------------------------------------------------------


def _split_top_commas(body: str, source: str, base_offset: int) -> List[Tuple[str, int]]:
    """Split a filter body on top-level commas.

    Respects ``[...]`` bracket nesting: commas inside brackets are part of the
    value list, not term separators. Returns ``(term, abs_offset_of_term_start)``
    pairs to support precise error positioning.
    """
    terms: List[Tuple[str, int]] = []
    depth = 0
    start = 0
    for i, ch in enumerate(body):
        if ch == "[":
            depth += 1
        elif ch == "]":
            if depth == 0:
                raise SelectorParseError(
                    "Unmatched ']'", source=source, position=base_offset + i
                )
            depth -= 1
        elif ch == "," and depth == 0:
            terms.append((body[start:i], base_offset + start))
            start = i + 1
    if depth != 0:
        raise SelectorParseError(
            "Unmatched '['", source=source, position=base_offset + body.rfind("[")
        )
    terms.append((body[start:], base_offset + start))
    return terms


def _parse_value_part(value: str, source: str, base_offset: int) -> Tuple[str, ...]:
    """Parse the right-hand side of ``key:<value>``.

    Returns a tuple of value strings (one element for single value, multiple for
    bracketed OR list). Each value may contain glob characters.
    """
    value = value.strip()
    if not value:
        raise SelectorParseError(
            "Empty value after ':'", source=source, position=base_offset
        )
    if value.startswith("["):
        if not value.endswith("]"):
            raise SelectorParseError(
                "Unclosed '['", source=source, position=base_offset
            )
        inner = value[1:-1].strip()
        if not inner:
            raise SelectorParseError(
                "Empty value list '[]'", source=source, position=base_offset
            )
        # Bracket bodies are flat (Phase 2 grammar); split on commas, strip, reject empties
        out: List[str] = []
        for raw in inner.split(","):
            v = raw.strip()
            if not v:
                raise SelectorParseError(
                    "Empty value in list", source=source, position=base_offset
                )
            if "[" in v or "]" in v:
                raise SelectorParseError(
                    "Nested brackets are not supported in this DSL version",
                    source=source,
                    position=base_offset,
                )
            out.append(v)
        return tuple(out)
    if "[" in value or "]" in value:
        raise SelectorParseError(
            "Stray bracket in value", source=source, position=base_offset
        )
    return (value,)


_KEY_PATTERN = ("0123456789"
                "abcdefghijklmnopqrstuvwxyz"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "_-.")


def _is_valid_key(key: str) -> bool:
    """Label keys are conservative identifiers: alnum, '_', '-', '.'."""
    return bool(key) and all(c in _KEY_PATTERN for c in key)


def _parse_filter_body(body: str, source: str, base_offset: int) -> Filter:
    """Parse the contents of one ``(...)`` block into a :class:`Filter`.

    Supports:
        ``*`` or empty body                -> match-all (empty Filter)
        ``key:value``                      -> single-value key filter
        ``key:[v1,v2]``                    -> OR within a key
        ``key:pattern*``                   -> glob value
        ``k1:v1,k2:v2``                    -> AND across keys
        bare string                        -> name match (id/name)
        bare + key:value                   -> name AND key constraints
    """
    stripped = body.strip()
    if not stripped or stripped == "*":
        return Filter()

    name_match: Optional[str] = None
    key_filters: List[KeyFilter] = []

    for term, term_offset in _split_top_commas(body, source, base_offset):
        # Account for leading whitespace inside the term when reporting positions.
        leading = len(term) - len(term.lstrip())
        term_stripped = term.strip()
        term_abs = term_offset + leading
        if not term_stripped:
            raise SelectorParseError(
                "Empty term (extra comma?)", source=source, position=term_abs
            )

        # Find a top-level ':' (one not inside the value brackets) to classify
        # bare-name vs key:value.
        colon_pos = -1
        depth = 0
        for j, ch in enumerate(term_stripped):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
            elif ch == ":" and depth == 0:
                colon_pos = j
                break

        if colon_pos < 0:
            # Bare term: name match or '*'
            if term_stripped == "*":
                continue  # vacuous, contributes nothing
            if name_match is not None:
                raise SelectorParseError(
                    f"Multiple bare-name terms ({name_match!r} and {term_stripped!r})",
                    source=source,
                    position=term_abs,
                )
            name_match = term_stripped
            continue

        key = term_stripped[:colon_pos].strip()
        value_part = term_stripped[colon_pos + 1:]
        value_offset = term_abs + colon_pos + 1
        if not _is_valid_key(key):
            raise SelectorParseError(
                f"Invalid key {key!r} (allowed: alphanumeric, '_', '-', '.')",
                source=source,
                position=term_abs,
            )
        values = _parse_value_part(value_part, source, value_offset)
        key_filters.append(KeyFilter(key=key, values=values))

    return Filter(name_match=name_match, key_filters=tuple(key_filters))


_VALID_SCOPES = ("device", "function", "event")


def _consume_scope(s: str, source: str, start: int) -> Tuple[str, Filter, int]:
    """Consume one ``<name>(<body>)`` from ``s`` starting at ``start``.

    Returns ``(scope_name, filter, position_after_closing_paren)``. Skips
    leading whitespace.
    """
    i = start
    n = len(s)
    while i < n and s[i].isspace():
        i += 1
    name_start = i
    while i < n and s[i] not in "( \t":
        i += 1
    name = s[name_start:i]
    if not name:
        raise SelectorParseError(
            "Expected scope name (device|function|event)", source=source, position=name_start
        )
    if name not in _VALID_SCOPES:
        raise SelectorParseError(
            f"Unknown scope {name!r} (expected one of {_VALID_SCOPES})",
            source=source,
            position=name_start,
        )
    while i < n and s[i].isspace():
        i += 1
    if i >= n or s[i] != "(":
        raise SelectorParseError(
            f"Expected '(' after scope {name!r}", source=source, position=i
        )
    body_start = i + 1
    # Find matching ')', tracking [...] nesting so a stray ')' inside brackets
    # would not be treated as the scope close. (Reserved chars rule out ')'
    # in valid values, but be defensive.)
    depth = 0
    last_open_bracket = -1
    j = body_start
    while j < n:
        ch = s[j]
        if ch == "[":
            depth += 1
            last_open_bracket = j
        elif ch == "]":
            depth -= 1
        elif ch == ")" and depth == 0:
            break
        j += 1
    if j >= n:
        if depth > 0:
            raise SelectorParseError(
                "Unclosed '['", source=source, position=last_open_bracket
            )
        raise SelectorParseError(
            f"Unclosed '(' for scope {name!r}", source=source, position=body_start - 1
        )
    body = s[body_start:j]
    flt = _parse_filter_body(body, source=source, base_offset=body_start)
    return name, flt, j + 1


def parse_selector(s: str) -> Selector:
    """Parse a selector string into a :class:`Selector`.

    Examples::

        parse_selector("device(category:camera)")
        parse_selector("device(category:[camera,robot], location:warehouse1/*)")
        parse_selector("device(*).function(direction:write)")
        parse_selector("function(safety:critical)")

    Raises :class:`SelectorParseError` on malformed input.
    """
    if not isinstance(s, str):
        raise SelectorParseError(f"Selector must be a string, got {type(s).__name__}")
    raw = s
    if not s.strip():
        raise SelectorParseError("Empty selector", source=raw, position=0)

    name1, filter1, after1 = _consume_scope(s, source=raw, start=0)

    # Optional ".scope(...)" extension
    i = after1
    n = len(s)
    while i < n and s[i].isspace():
        i += 1

    if i >= n:
        # Single-scope selector
        if name1 == "device":
            return Selector(scope=Scope.DEVICE_ONLY, device=filter1, raw=raw)
        if name1 == "function":
            return Selector(scope=Scope.FUNCTION_ONLY, function=filter1, raw=raw)
        if name1 == "event":
            return Selector(scope=Scope.EVENT_ONLY, event=filter1, raw=raw)
        # _consume_scope already validated name1
        raise SelectorParseError(f"Internal: unhandled scope {name1!r}", source=raw)

    if s[i] != ".":
        raise SelectorParseError(
            f"Unexpected character {s[i]!r} after scope", source=raw, position=i
        )

    name2, filter2, after2 = _consume_scope(s, source=raw, start=i + 1)

    # Trailing content?
    j = after2
    while j < n and s[j].isspace():
        j += 1
    if j < n:
        raise SelectorParseError(
            f"Unexpected trailing content {s[j:]!r}", source=raw, position=j
        )

    if name1 != "device":
        raise SelectorParseError(
            f"Chained scopes must start with 'device', got {name1!r}",
            source=raw,
            position=0,
        )
    if name2 == "function":
        return Selector(
            scope=Scope.DEVICE_FUNCTION, device=filter1, function=filter2, raw=raw
        )
    if name2 == "event":
        return Selector(
            scope=Scope.DEVICE_EVENT, device=filter1, event=filter2, raw=raw
        )
    raise SelectorParseError(
        f"Cannot chain device(...).{name2}(...); expected 'function' or 'event'",
        source=raw,
        position=i + 1,
    )
