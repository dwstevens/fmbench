"""Schema construction in the Apple `fm` JSON dialect.

The `fm` schema dialect is *almost* standard JSON Schema, with one hard requirement:
every object MUST carry an ``x-order`` array listing its property order. A schema
missing ``x-order`` is rejected with "data couldn't be read because it is missing".

`fm schema object` (the CLI builder) emits valid skeletons but exposes no ``enum`` or
``const`` flags. The underlying constrained decoder *does* honor both, so we build
schemas here directly — getting the ``x-order`` requirement right and adding
``enum``/``const`` where we want hard constraints.

This module exposes a small builder, plus a registry of named schemas the cases use.
"""
from __future__ import annotations

import json
import os
from typing import Any


# ----------------------------------------------------------------------------
# Primitive property builders
# ----------------------------------------------------------------------------
def p_str(*, enum: list[str] | None = None, const: str | None = None,
          desc: str | None = None) -> dict[str, Any]:
    s: dict[str, Any] = {"type": "string"}
    if const is not None:
        s["const"] = const
    if enum is not None:
        s["enum"] = enum
    if desc:
        s["description"] = desc
    return s


def p_int(*, desc: str | None = None) -> dict[str, Any]:
    s: dict[str, Any] = {"type": "integer"}
    if desc:
        s["description"] = desc
    return s


def p_num(*, desc: str | None = None) -> dict[str, Any]:
    s: dict[str, Any] = {"type": "number"}
    if desc:
        s["description"] = desc
    return s


def p_bool(*, desc: str | None = None) -> dict[str, Any]:
    s: dict[str, Any] = {"type": "boolean"}
    if desc:
        s["description"] = desc
    return s


def p_arr(items: dict[str, Any], *, desc: str | None = None) -> dict[str, Any]:
    s: dict[str, Any] = {"type": "array", "items": items}
    if desc:
        s["description"] = desc
    return s


def ref(name: str) -> dict[str, Any]:
    return {"$ref": f"#/$defs/{name}"}


# ----------------------------------------------------------------------------
# Object / union builders
# ----------------------------------------------------------------------------
def obj(title: str, props: dict[str, dict], *, required: list[str] | None = None,
        defs: dict[str, dict] | None = None) -> dict[str, Any]:
    """Build an fm-dialect object schema.

    ``props`` order defines ``x-order``. ``required`` defaults to all keys; pass an
    explicit subset to make some fields optional (present in properties/x-order but
    not required).
    """
    order = list(props.keys())
    schema: dict[str, Any] = {
        "title": title,
        "type": "object",
        "additionalProperties": False,
        "required": order if required is None else required,
        "x-order": order,
        "properties": props,
    }
    if defs:
        schema["$defs"] = defs
    return schema


def union(title: str, defs: dict[str, dict]) -> dict[str, Any]:
    """Build an anyOf union root over named ``$defs`` — i.e. a tool router."""
    return {
        "title": title,
        "$defs": defs,
        "anyOf": [ref(name) for name in defs],
    }


# ----------------------------------------------------------------------------
# Tool definitions (used by routing + failure-mode suites)
# ----------------------------------------------------------------------------
def _tool_defs() -> dict[str, dict]:
    """The five tools, as object schemas. Each pins its name with ``const``."""
    return {
        "get_weather": obj("get_weather", {
            "tool": p_str(const="get_weather"),
            "location": p_str(desc="City name"),
            "unit": p_str(enum=["celsius", "fahrenheit"]),
            "days": p_int(desc="Number of forecast days"),
        }, required=["tool", "location", "unit"]),  # days optional

        "send_email": obj("send_email", {
            "tool": p_str(const="send_email"),
            "to": p_str(desc="Recipient email address"),
            "subject": p_str(),
            "body": p_str(),
            "cc": p_arr(p_str(), desc="CC email addresses"),
        }, required=["tool", "to", "subject", "body"]),  # cc optional

        "create_event": obj("create_event", {
            "tool": p_str(const="create_event"),
            "title": p_str(),
            "date": p_str(desc="ISO date YYYY-MM-DD"),
            "startTime": p_str(desc="24-hour HH:MM"),
            "durationMinutes": p_int(),
            "attendees": p_arr(p_str(), desc="Attendee names or emails"),
            "location": p_str(),
        }, required=["tool", "title", "date", "startTime", "durationMinutes", "attendees"]),

        "set_reminder": obj("set_reminder", {
            "tool": p_str(const="set_reminder"),
            "text": p_str(),
            "datetime": p_str(desc="ISO 8601 datetime"),
            "priority": p_str(enum=["low", "medium", "high"]),
        }),

        "play_music": obj("play_music", {
            "tool": p_str(const="play_music"),
            "query": p_str(desc="Artist, song, album, or playlist"),
            "shuffle": p_bool(),
            "volume": p_int(desc="0-100"),
        }, required=["tool", "query", "shuffle"]),  # volume optional
    }


def _respond_directly() -> dict[str, dict]:
    return {
        "respond_directly": obj("respond_directly", {
            "tool": p_str(const="respond_directly"),
            "message": p_str(desc="A direct natural-language answer when no tool fits"),
        }),
    }


# ----------------------------------------------------------------------------
# Extraction schemas
# ----------------------------------------------------------------------------
def _order_schema() -> dict[str, Any]:
    line_item = obj("LineItem", {
        "name": p_str(),
        "quantity": p_int(),
        "unitPrice": p_num(desc="Price per unit in USD"),
    })
    customer = obj("Customer", {
        "name": p_str(),
        "email": p_str(),
        "vip": p_bool(desc="true if a VIP / loyalty member"),
    })
    return obj("Order", {
        "orderId": p_str(desc="Any short id"),
        "customer": ref("Customer"),
        "items": p_arr(ref("LineItem")),
        "total": p_num(desc="Sum of quantity*unitPrice across all items, in USD"),
        "priority": p_str(enum=["standard", "express", "overnight"]),
    }, defs={"LineItem": line_item, "Customer": customer})


def _profile_schema() -> dict[str, Any]:
    address = obj("Address", {
        "street": p_str(),
        "city": p_str(),
        "country": p_str(),
        "postalCode": p_str(),
    }, required=["city", "country"])  # street/postal optional
    job = obj("Job", {
        "title": p_str(),
        "company": p_str(),
        "years": p_num(desc="Years in this role"),
    })
    return obj("Profile", {
        "fullName": p_str(),
        "age": p_int(),
        "address": ref("Address"),
        "currentJob": ref("Job"),
        "skills": p_arr(p_str()),
        "remote": p_bool(desc="true if works remotely"),
    }, required=["fullName", "address", "skills"], defs={"Address": address, "Job": job})


def _recipe_schema() -> dict[str, Any]:
    ingredient = obj("Ingredient", {
        "item": p_str(),
        "amount": p_num(),
        "unit": p_str(enum=["g", "ml", "cup", "tbsp", "tsp", "piece"]),
    })
    nutrition = obj("Nutrition", {
        "calories": p_int(),
        "protein": p_num(desc="grams"),
    })
    return obj("Recipe", {
        "name": p_str(),
        "servings": p_int(),
        "difficulty": p_str(enum=["easy", "medium", "hard"]),
        "ingredients": p_arr(ref("Ingredient")),
        "steps": p_arr(p_str()),
        "nutrition": ref("Nutrition"),
    }, required=["name", "servings", "difficulty", "ingredients", "steps"],
        defs={"Ingredient": ingredient, "Nutrition": nutrition})


def _ticket_schema() -> dict[str, Any]:
    """Single-action schema heavy on enums + const — used by the constraints suite."""
    return obj("Ticket", {
        "tool": p_str(const="create_ticket"),
        "title": p_str(),
        "severity": p_str(enum=["sev1", "sev2", "sev3", "sev4"],
                          desc="sev1=critical outage ... sev4=cosmetic"),
        "status": p_str(enum=["open", "in_progress", "blocked", "closed"]),
        "component": p_str(enum=["frontend", "backend", "database", "infra", "auth"]),
        "estimateHours": p_num(),
    }, required=["tool", "title", "severity", "status", "component"])


def _weather_strict() -> dict[str, Any]:
    """Const tool + two enums — the adversarial enum-enforcement target."""
    return obj("WeatherStrict", {
        "tool": p_str(const="get_weather"),
        "location": p_str(),
        "unit": p_str(enum=["celsius", "fahrenheit"]),
        "urgency": p_str(enum=["low", "normal", "high"]),
    })


# ----------------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------------
def build_all() -> dict[str, dict]:
    return {
        "tools_basic": union("ToolCall", _tool_defs()),
        "tools_with_noop": union("ToolCall", {**_tool_defs(), **_respond_directly()}),
        "order": _order_schema(),
        "profile": _profile_schema(),
        "recipe": _recipe_schema(),
        "ticket": _ticket_schema(),
        "weather_strict": _weather_strict(),
    }


def write_all(dirpath: str) -> dict[str, str]:
    """Write every registered schema to ``dirpath`` and return key -> file path."""
    os.makedirs(dirpath, exist_ok=True)
    paths: dict[str, str] = {}
    for key, schema in build_all().items():
        path = os.path.join(dirpath, f"{key}.json")
        with open(path, "w") as fh:
            json.dump(schema, fh, indent=2)
        paths[key] = path
    return paths


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "schemas"
    for k, p in write_all(out).items():
        print(f"wrote {p}")
