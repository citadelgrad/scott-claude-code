#!/usr/bin/env python3
"""Generate the committed stdlib-only validator from frozen schemas.

Sources: JSON Schema Draft 2020-12 Core §§8, 9, 10 and Validation §§6.
https://json-schema.org/draft/2020-12/json-schema-core.html
https://json-schema.org/draft/2020-12/json-schema-validation.html
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import re
import tempfile
from pathlib import Path
from typing import Any

DRAFT = "https://json-schema.org/draft/2020-12/schema"
EXPECTED = (
    "approval-record-v1.schema.json",
    "checkpoint-pointer-v1.schema.json",
    "direct-operation-record-v1.schema.json",
    "direct-operation-request-v1.schema.json",
    "durable-executor-result-v1.schema.json",
    "durable-handoff-v1.schema.json",
    "evaluation-result-v1.schema.json",
    "harness-receipt-v1.schema.json",
    "lane-freeze-v1.schema.json",
    "native-command-event-v1.schema.json",
    "operation-journal-event-v1.schema.json",
    "operation-result-v1.schema.json",
    "ownership-history-event-v1.schema.json",
    "ownership-record-v1.schema.json",
    "parent-verification-v1.schema.json",
    "pending-action-v1.schema.json",
    "recovery-probe-v1.schema.json",
    "reviewer-result-v1.schema.json",
    "run-checkpoint-v1.schema.json",
    "run-manifest-v1.schema.json",
    "run-request-v1.schema.json",
    "safe-command-result-v1.schema.json",
    "worker-execution-result-v1.schema.json",
    "worker-packet-v1.schema.json",
)
ALLOWED = frozenset(
    {
        "$schema",
        "$id",
        "$defs",
        "$ref",
        "type",
        "const",
        "enum",
        "properties",
        "required",
        "additionalProperties",
        "propertyNames",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "dependentRequired",
        "title",
        "description",
        "$comment",
    }
)
SCHEMA_MAP_KEYS = {"properties", "$defs"}
SCHEMA_ARRAY_KEYS = {"allOf", "anyOf", "oneOf", "prefixItems"}
SCHEMA_SINGLE_KEYS = {
    "additionalProperties",
    "propertyNames",
    "items",
    "not",
    "if",
    "then",
    "else",
}


class SourceError(ValueError):
    pass


def _reject_float(_: str) -> Any:
    raise SourceError("lexical floating-point values are forbidden")


def _reject_constant(_: str) -> Any:
    raise SourceError("non-finite constants are forbidden")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourceError(f"duplicate key: {key}")
        result[key] = value
    return result


# RFC 8259 §8.2 warns that unpaired surrogate escapes have unpredictable behavior.
# https://www.rfc-editor.org/rfc/rfc8259#section-8.2
def strict_loads(raw: bytes) -> Any:
    if b"\x00" in raw:
        raise SourceError("NUL is forbidden")
    try:
        text = raw.decode("utf-8", "strict")
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError("invalid JSON") from exc
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if any(0xD800 <= ord(ch) <= 0xDFFF for ch in item):
                raise SourceError("unpaired surrogate is forbidden")
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return value


def _check_node(node: Any, path: str = "") -> None:
    if isinstance(node, bool):
        raise SourceError(f"boolean schema unsupported at {path or '/'}")
    if not isinstance(node, dict):
        raise SourceError(f"schema must be object at {path or '/'}")
    unknown = set(node) - ALLOWED
    if unknown:
        raise SourceError(f"unsupported keyword at {path or '/'}: {sorted(unknown)!r}")
    if "pattern" in node:
        pattern = node["pattern"]
        if not isinstance(pattern, str) or re.search(
            r"\\[1-9]|\(\?P|\(\?<=[^)]|\(\?<!|\(\?[aiLmsux-]", pattern
        ):
            raise SourceError(f"unsupported regular expression at {path}/pattern")
        re.compile(pattern)
    for key in SCHEMA_MAP_KEYS:
        for name, child in node.get(key, {}).items():
            _check_node(child, f"{path}/{key}/{name}")
    for key in SCHEMA_ARRAY_KEYS:
        for index, child in enumerate(node.get(key, [])):
            _check_node(child, f"{path}/{key}/{index}")
    for key in SCHEMA_SINGLE_KEYS:
        child = node.get(key)
        if isinstance(child, dict):
            _check_node(child, f"{path}/{key}")
        elif child not in (None, True, False):
            raise SourceError(f"invalid subschema at {path}/{key}")


def load_sources(schema_dir: Path) -> tuple[dict[str, Any], dict[str, str]]:
    names = tuple(sorted(p.name for p in schema_dir.glob("*.schema.json")))
    if names != EXPECTED:
        raise SourceError(
            f"schema inventory mismatch: expected {EXPECTED!r}, got {names!r}"
        )
    schemas: dict[str, Any] = {}
    hashes: dict[str, str] = {}
    ids: dict[str, str] = {}
    for name in names:
        raw = (schema_dir / name).read_bytes()
        value = strict_loads(raw)
        if not isinstance(value, dict) or value.get("$schema") != DRAFT:
            raise SourceError(f"{name}: exact Draft 2020-12 declaration required")
        record = name.removesuffix("-v1.schema.json")
        expected_id = f"urn:citadelgrad:scott-cc:beads:{record}:v1"
        if value.get("$id") != expected_id:
            raise SourceError(f"{name}: expected $id {expected_id}")
        if expected_id in ids:
            raise SourceError(f"duplicate $id: {expected_id}")
        _check_node(value)
        schemas[name] = value
        hashes[name] = hashlib.sha256(raw).hexdigest()
        ids[expected_id] = name
    for name, value in schemas.items():
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                ref = item.get("$ref")
                if isinstance(ref, str) and not ref.startswith("#"):
                    base = ref.split("#", 1)[0]
                    if base not in ids:
                        raise SourceError(f"{name}: unresolved or remote $ref {ref}")
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource

        resources = [
            (schema["$id"], Resource.from_contents(schema))
            for schema in schemas.values()
        ]
        registry = Registry().with_resources(resources)
        for schema in schemas.values():
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema, registry=registry)
    except ImportError as exc:
        raise SourceError("jsonschema>=4.26,<5 is required to generate") from exc
    return schemas, hashes


RUNTIME = r"""# Generated by generate_schema_runtime.py; DO NOT EDIT.
# Runtime source semantics: JSON Schema Draft 2020-12 Core/Validation.
from __future__ import annotations
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

# fmt: off
SCHEMAS = json.loads(__SCHEMAS__)
SOURCE_HASHES = json.loads(__HASHES__)
MANIFEST_SHA256 = __MANIFEST__
# fmt: on
MAX_JSON_BYTES = 65536


@dataclass(frozen=True, order=True)
class Finding:
    code: str
    instance_path: str
    schema_path: str
    message: str


class ValidationFailure(ValueError):
    def __init__(self, findings: list[Finding]):
        super().__init__("schema validation failed")
        self.findings = tuple(findings)


class JsonLoadFailure(ValueError):
    pass


def _float(_: str):
    raise JsonLoadFailure("JSON_FLOAT_FORBIDDEN")


def _constant(_: str):
    raise JsonLoadFailure("JSON_CONSTANT_FORBIDDEN")


def _integer(value: str):
    if len(value.lstrip("-")) > 128:
        raise JsonLoadFailure("JSON_INTEGER_TOO_LONG")
    return int(value)


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise JsonLoadFailure("JSON_DUPLICATE_KEY")
        out[key] = value
    return out


# Reject unpaired surrogates per RFC 8259 §8.2.
# https://www.rfc-editor.org/rfc/rfc8259#section-8.2
def strict_json_loads(raw: bytes, *, max_bytes: int = MAX_JSON_BYTES):
    if len(raw) > max_bytes:
        raise JsonLoadFailure("JSON_TOO_LARGE")
    if b"\x00" in raw:
        raise JsonLoadFailure("JSON_NUL_FORBIDDEN")
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_pairs,
            parse_float=_float,
            parse_int=_integer,
            parse_constant=_constant,
        )
    except JsonLoadFailure:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JsonLoadFailure("JSON_MALFORMED") from exc
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 128 or count > 100000:
            raise JsonLoadFailure("JSON_COMPLEXITY_LIMIT")
        if isinstance(item, str):
            if "\x00" in item:
                raise JsonLoadFailure("JSON_NUL_FORBIDDEN")
            if any(0xD800 <= ord(ch) <= 0xDFFF for ch in item):
                raise JsonLoadFailure("JSON_SURROGATE_FORBIDDEN")
            if len(item.encode("utf-8")) > max_bytes:
                raise JsonLoadFailure("JSON_STRING_TOO_LARGE")
        elif isinstance(item, dict):
            stack.extend((x, depth + 1) for pair in item.items() for x in pair)
        elif isinstance(item, list):
            stack.extend((x, depth + 1) for x in item)
    return value


def _esc(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def _same(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _type_ok(kind, value):
    return {
        "object": type(value) is dict,
        "array": type(value) is list,
        "string": type(value) is str,
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "boolean": type(value) is bool,
        "null": value is None,
    }.get(kind, False)


def _resolve(ref, current):
    if ref.startswith("#"):
        target = current
        frag = ref[1:]
    else:
        base, _, frag = ref.partition("#")
        target = next((s for s in SCHEMAS.values() if s.get("$id") == base), None)
        if target is None:
            raise RuntimeError("unresolved frozen schema reference")
    for token in frag.lstrip("/").split("/") if frag else ():
        token = token.replace("~1", "/").replace("~0", "~")
        target = target[token]
    return target


def _err(code, ip, sp, msg):
    return Finding(code, ip or "", sp or "", msg)


def _validate(schema, value, ip, sp, root):
    out = []
    if "$ref" in schema:
        return _validate(
            _resolve(schema["$ref"], root),
            value,
            ip,
            sp + "/$ref",
            _resolve(schema["$ref"], root),
        )
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(k, value) for k in kinds):
            return [_err("SCHEMA_TYPE", ip, sp + "/type", "JSON type is not allowed")]
    if "const" in schema and not _same(value, schema["const"]):
        out.append(
            _err(
                "SCHEMA_CONST",
                ip,
                sp + "/const",
                "value differs from required constant",
            )
        )
    if "enum" in schema and not any(_same(value, x) for x in schema["enum"]):
        out.append(
            _err("SCHEMA_ENUM", ip, sp + "/enum", "value is not an allowed enum member")
        )
    if type(value) is str:
        if len(value) < schema.get("minLength", 0):
            out.append(
                _err("SCHEMA_MIN_LENGTH", ip, sp + "/minLength", "string is too short")
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(
                _err("SCHEMA_MAX_LENGTH", ip, sp + "/maxLength", "string is too long")
            )
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            out.append(
                _err(
                    "SCHEMA_PATTERN",
                    ip,
                    sp + "/pattern",
                    "string does not match pattern",
                )
            )
    if type(value) is int:
        if "minimum" in schema and value < schema["minimum"]:
            out.append(
                _err("SCHEMA_MINIMUM", ip, sp + "/minimum", "number is below minimum")
            )
        if "maximum" in schema and value > schema["maximum"]:
            out.append(
                _err("SCHEMA_MAXIMUM", ip, sp + "/maximum", "number is above maximum")
            )
    if type(value) is list:
        if len(value) < schema.get("minItems", 0):
            out.append(
                _err(
                    "SCHEMA_MIN_ITEMS", ip, sp + "/minItems", "array has too few items"
                )
            )
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(
                _err(
                    "SCHEMA_MAX_ITEMS", ip, sp + "/maxItems", "array has too many items"
                )
            )
        if schema.get("uniqueItems") and any(
            _same(value[i], value[j]) for i in range(len(value)) for j in range(i)
        ):
            out.append(
                _err(
                    "SCHEMA_UNIQUE_ITEMS",
                    ip,
                    sp + "/uniqueItems",
                    "array items are not unique",
                )
            )
        prefix = schema.get("prefixItems", [])
        for i, sub in enumerate(prefix[: len(value)]):
            out.extend(
                _validate(sub, value[i], f"{ip}/{i}", f"{sp}/prefixItems/{i}", root)
            )
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value[len(prefix) :], len(prefix)):
                out.extend(
                    _validate(schema["items"], item, f"{ip}/{i}", sp + "/items", root)
                )
    if type(value) is dict:
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                out.append(
                    _err(
                        "SCHEMA_REQUIRED",
                        ip,
                        sp + "/required",
                        "required property is missing",
                    )
                )
        for key, item in value.items():
            if key in props:
                out.extend(
                    _validate(
                        props[key],
                        item,
                        f"{ip}/{_esc(key)}",
                        f"{sp}/properties/{_esc(key)}",
                        root,
                    )
                )
            elif schema.get("additionalProperties") is False:
                out.append(
                    _err(
                        "SCHEMA_ADDITIONAL_PROPERTIES",
                        f"{ip}/{_esc(key)}",
                        sp + "/additionalProperties",
                        "additional property is forbidden",
                    )
                )
            elif isinstance(schema.get("additionalProperties"), dict):
                out.extend(
                    _validate(
                        schema["additionalProperties"],
                        item,
                        f"{ip}/{_esc(key)}",
                        sp + "/additionalProperties",
                        root,
                    )
                )
            if isinstance(schema.get("propertyNames"), dict):
                out.extend(
                    _validate(
                        schema["propertyNames"],
                        key,
                        f"{ip}/{_esc(key)}",
                        sp + "/propertyNames",
                        root,
                    )
                )
        for key, needs in schema.get("dependentRequired", {}).items():
            if key in value:
                for needed in needs:
                    if needed not in value:
                        out.append(
                            _err(
                                "SCHEMA_DEPENDENT_REQUIRED",
                                ip,
                                sp + "/dependentRequired",
                                "dependent property is missing",
                            )
                        )
    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema:
            branches = [
                _validate(sub, value, ip, f"{sp}/{key}/{i}", root)
                for i, sub in enumerate(schema[key])
            ]
            matches = sum(not branch for branch in branches)
            if key == "allOf":
                out.extend(x for branch in branches for x in branch)
            elif key == "anyOf" and not matches:
                out.append(
                    _err("SCHEMA_ANY_OF", ip, sp + "/anyOf", "no branch matched")
                )
            elif key == "oneOf" and matches != 1:
                out.append(
                    _err(
                        "SCHEMA_ONE_OF",
                        ip,
                        sp + "/oneOf",
                        "exactly one branch must match",
                    )
                )
    if "not" in schema and not _validate(schema["not"], value, ip, sp + "/not", root):
        out.append(_err("SCHEMA_NOT", ip, sp + "/not", "forbidden schema matched"))
    if "if" in schema:
        chosen = (
            "then"
            if not _validate(schema["if"], value, ip, sp + "/if", root)
            else "else"
        )
        if chosen in schema:
            out.extend(_validate(schema[chosen], value, ip, sp + "/" + chosen, root))
    return out


_RANK = {
    name: i
    for i, name in enumerate(
        (
            "SCHEMA_REQUIRED",
            "SCHEMA_TYPE",
            "SCHEMA_CONST",
            "SCHEMA_ENUM",
            "SCHEMA_PATTERN",
            "SCHEMA_MIN_LENGTH",
            "SCHEMA_MAX_LENGTH",
            "SCHEMA_MINIMUM",
            "SCHEMA_MAXIMUM",
            "SCHEMA_MIN_ITEMS",
            "SCHEMA_MAX_ITEMS",
            "SCHEMA_UNIQUE_ITEMS",
            "SCHEMA_ADDITIONAL_PROPERTIES",
            "SCHEMA_DEPENDENT_REQUIRED",
            "SCHEMA_NOT",
            "SCHEMA_ONE_OF",
            "SCHEMA_ANY_OF",
        )
    )
}


def validate_instance(schema_name, value):
    if schema_name not in SCHEMAS:
        raise KeyError(schema_name)
    findings = _validate(SCHEMAS[schema_name], value, "", "", SCHEMAS[schema_name])
    return sorted(
        set(findings),
        key=lambda x: (x.instance_path, _RANK.get(x.code, 999), x.schema_path, x.code),
    )


def require_valid(schema_name, value):
    findings = validate_instance(schema_name, value)
    if findings:
        raise ValidationFailure(findings)


def read_bounded(path, max_bytes=MAX_JSON_BYTES):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError as exc:
        raise JsonLoadFailure("JSON_READ_ERROR") from exc
    if len(raw) > max_bytes:
        raise JsonLoadFailure("JSON_TOO_LARGE")
    return raw


def load_and_validate(path, schema_name, max_bytes=MAX_JSON_BYTES):
    value = strict_json_loads(read_bounded(path, max_bytes), max_bytes=max_bytes)
    require_valid(schema_name, value)
    return value


def schema_source_hash(schema_name):
    return SOURCE_HASHES[schema_name]


def assert_generated_current(schema_dir):
    for name, digest in SOURCE_HASHES.items():
        if hashlib.sha256((Path(schema_dir) / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("generated schema runtime is stale")
"""


def render(schema_dir: Path) -> bytes:
    schemas, hashes = load_sources(schema_dir)
    schema_json = json.dumps(
        schemas, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    hash_json = json.dumps(hashes, sort_keys=True, separators=(",", ":"))
    manifest = hashlib.sha256((schema_json + "\n" + hash_json).encode()).hexdigest()
    text = (
        RUNTIME.replace("__SCHEMAS__", repr(schema_json))
        .replace("__HASHES__", repr(hash_json))
        .replace("__MANIFEST__", repr(manifest))
    )
    return text.encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--schema-dir", type=Path, default=Path(__file__).parents[1] / "schemas"
    )
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).with_name("schema_runtime.py")
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        generated = render(args.schema_dir)
    except (OSError, SourceError, ValueError) as exc:
        print(f"schema generation failed: {exc}", file=sys.stderr)
        return 1
    if args.check:
        try:
            current = args.output.read_bytes()
        except OSError:
            return 1
        if current != generated:
            print("generated schema runtime is stale", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{args.output.name}.", dir=args.output.parent
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(generated)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.replace(temporary, args.output)
        directory_fd = os.open(
            args.output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
