"""The frontend's API types are generated from the API's own schema, and that schema is accurate."""

import json
from typing import Any
import unittest

from scripts.export_openapi import OPENAPI_PATH, openapi_json


REGENERATE = "Run `uv run --locked python scripts/export_openapi.py` and `npm --prefix frontend run api:types`."


def response_schemas(document: dict[str, Any]) -> set[str]:
    """Names of the component schemas that successful responses contain, directly or nested."""
    schemas = document["components"]["schemas"]
    found: set[str] = set()

    def visit(node: object) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                name = ref.removeprefix("#/components/schemas/")
                if name not in found:
                    found.add(name)
                    visit(schemas[name])
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    for operations in document["paths"].values():
        for operation in operations.values():
            for code, response in operation["responses"].items():
                if code.startswith("2"):
                    visit(response)
    return found


class ApiContractTests(unittest.TestCase):
    def test_the_frontend_schema_matches_the_api(self):
        self.assertTrue(
            OPENAPI_PATH.read_text() == openapi_json(), f"frontend/openapi.json differs from the API. {REGENERATE}",
        )

    def test_a_response_schema_marks_every_field_required(self):
        # The API always sends every field. A field marked optional would make the generated
        # types say it may be missing. Derive response models from ApiModel.
        document = json.loads(openapi_json())
        sent = response_schemas(document)
        self.assertTrue({"JobSnapshot", "ProgressEvent", "AnalysisResult", "Health", "AuthSession"} <= sent)
        optional = {
            name: sorted(set(schema["properties"]) - set(schema.get("required", [])))
            for name, schema in document["components"]["schemas"].items()
            if name in sent and "properties" in schema
        }
        self.assertEqual({name: fields for name, fields in optional.items() if fields}, {})
