import asyncio
from typing import Any

import httpx
from fastapi import FastAPI

from app.config import BOARD_CONTRACT, Settings
from app.database import create_database_engine
from app.main import create_app


def get_health(application: FastAPI) -> httpx.Response:
    async def request_health() -> httpx.Response:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.get("/health")

    return asyncio.run(request_health())


def runtime_settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://user:password@localhost/sales",
        monday_ingestion_access_token="token",
        monday_webhook_shared_secret="shared-secret",
        gemini_api_key="gemini-key",
        gemini_model="gemini-test-model",
    )


def valid_board() -> dict[str, Any]:
    return {
        "id": BOARD_CONTRACT.sales_board_id,
        "columns": [
            {"id": BOARD_CONTRACT.email_file_column_id, "type": "file"},
            {
                "id": BOARD_CONTRACT.accounts_relation_column_id,
                "type": "board_relation",
                "settings": {"board_ids": [BOARD_CONTRACT.accounts_board_id]},
            },
            {
                "id": BOARD_CONTRACT.postcode_column_id,
                "type": "dropdown",
                "settings": {
                    "labels": [
                        {"id": label.id, "name": label.name}
                        for label in BOARD_CONTRACT.required_postcode_labels
                    ]
                },
            },
        ],
    }


def test_startup_enables_publication_only_after_schema_validation() -> None:
    requested_board_ids: list[int] = []

    def load_schema(board_id: int) -> dict[str, Any]:
        requested_board_ids.append(board_id)
        return valid_board()

    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    application = create_app(
        settings=runtime_settings(), schema_loader=load_schema, engine=engine
    )
    response = get_health(application)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "publication_enabled": True,
        "publication_gate_issues": [],
    }
    assert requested_board_ids == [BOARD_CONTRACT.sales_board_id]
    engine.dispose()


def test_schema_failure_keeps_service_available_but_publication_disabled() -> None:
    def fail_to_load(_: int) -> dict[str, Any]:
        raise TimeoutError("Monday did not respond")

    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    application = create_app(
        settings=runtime_settings(), schema_loader=fail_to_load, engine=engine
    )
    response = get_health(application)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "publication_enabled": False,
        "publication_gate_issues": ["schema_fetch_failed"],
    }
    engine.dispose()


def test_startup_validates_the_runtime_sales_board_id() -> None:
    settings = runtime_settings().model_copy(update={"sales_board_id": 123})
    requested_board_ids: list[int] = []

    def load_schema(board_id: int) -> dict[str, Any]:
        requested_board_ids.append(board_id)
        return {"id": board_id, "columns": []}

    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    application = create_app(settings=settings, schema_loader=load_schema, engine=engine)
    response = get_health(application)

    assert response.status_code == 200
    assert requested_board_ids == [123]
    engine.dispose()