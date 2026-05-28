"""
Programmatic WhatsApp Flow schema for Visitor Request.

This keeps the Flow JSON in code so it can be versioned and regenerated.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


def build_visitor_flow_schema() -> dict[str, Any]:
    """Return a 2-screen visitor flow schema."""
    return {
        "version": "7.1",
        "screens": [
            {
                "id": "VISIT_DETAILS",
                "title": "Visitor Request",
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [
                        {
                            "type": "Form",
                            "name": "visit_form",
                            "children": [
                                {
                                    "type": "DatePicker",
                                    "name": "coming_on",
                                    "label": "Coming On",
                                    "required": True,
                                },
                                {
                                    "type": "TextInput",
                                    "name": "coming_from",
                                    "label": "Coming From",
                                    "required": True,
                                    "placeholder": "Enter location/company name",
                                },
                                {
                                    "type": "Dropdown",
                                    "name": "purpose",
                                    "label": "Purpose of Visit",
                                    "required": True,
                                    "data-source": [
                                        {"id": "customer_visit", "title": "Customer Visit"},
                                        {"id": "other", "title": "Other"},
                                    ],
                                },
                                {
                                    "type": "TextInput",
                                    "name": "other_purpose",
                                    "label": "Enter Purpose",
                                    "required": True,
                                    "visible_if": {"purpose": "other"},
                                },
                                {
                                    "type": "Footer",
                                    "label": "Next",
                                    "on-click-action": {
                                        "name": "navigate",
                                        "next": {"type": "screen", "name": "VISITOR_DETAILS"},
                                    },
                                },
                            ],
                        }
                    ],
                },
            },
            {
                "id": "VISITOR_DETAILS",
                "title": "Visitor Details",
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [
                        {
                            "type": "Form",
                            "name": "visitor_form",
                            "children": [
                                {
                                    "type": "TextInput",
                                    "name": "no_of_people",
                                    "label": "No of People",
                                    "input-type": "number",
                                    "required": True,
                                },
                                {
                                    "type": "TextInput",
                                    "name": "visitor_name",
                                    "label": "Name of Visitor",
                                    "required": True,
                                },
                                {
                                    "type": "TextInput",
                                    "name": "visitor_mobile",
                                    "label": "Visitor Mobile Number",
                                    "input-type": "phone",
                                    "required": True,
                                },
                                {
                                    "type": "Footer",
                                    "label": "Submit",
                                    "on-click-action": {
                                        "name": "complete",
                                        "payload": {
                                            "coming_on": "${form.coming_on}",
                                            "coming_from": "${form.coming_from}",
                                            "purpose": "${form.purpose}",
                                            "other_purpose": "${form.other_purpose}",
                                            "no_of_people": "${form.no_of_people}",
                                            "visitor_name": "${form.visitor_name}",
                                            "visitor_mobile": "${form.visitor_mobile}",
                                        },
                                    },
                                },
                            ],
                        }
                    ],
                },
            },
        ],
    }


def flow_json_text(indent: int = 2) -> str:
    return json.dumps(build_visitor_flow_schema(), indent=indent, ensure_ascii=True) + "\n"


def write_flow_json(path: str | Path) -> Path:
    out = Path(path)
    out.write_text(flow_json_text(), encoding="utf-8")
    return out


def for_provider(provider: str = "meta") -> dict[str, Any]:
    """
    Hook for provider-specific tweaks (Meta / Interakt imports).
    Currently returns base schema unchanged.
    """
    _ = provider
    return deepcopy(build_visitor_flow_schema())

