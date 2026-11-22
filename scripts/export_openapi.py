"""Save the API's OpenAPI schema, which the frontend's TypeScript types are generated from.

Run both commands after changing a model the API returns:

    uv run --locked python scripts/export_openapi.py
    npm --prefix frontend run api:types

tests/test_api_contract.py fails while frontend/openapi.json differs from the API, and
`npm --prefix frontend run typecheck` fails while the types differ from the saved schema.
"""

import json
from pathlib import Path
import sys

from backend.app.config import Settings
from backend.app.main import create_app


OPENAPI_PATH = Path(__file__).resolve().parent.parent / "frontend" / "openapi.json"


def openapi_json() -> str:
    """The schema in the default configuration, which reads no .env and opens no connection.

    The sign-out route exists only when GitHub sign-in is configured, so it is not listed.
    It returns no body, so the frontend needs no type for it.
    """
    return json.dumps(create_app(Settings()).openapi(), indent=2) + "\n"


if __name__ == "__main__":
    if sys.argv[1:]:
        raise SystemExit("This script takes no command-line arguments.")
    OPENAPI_PATH.write_text(openapi_json())
    print(f"Wrote {OPENAPI_PATH}")
