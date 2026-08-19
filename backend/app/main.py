"""FastAPI application and resource lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from starlette.concurrency import run_in_threadpool

from app.config import Settings, get_settings
from app.database import create_database_engine, create_session_factory
from app.monday_client import MondayClient
from app.publication_gate import validate_schema_at_startup


SchemaLoader = Callable[[int], Mapping[str, Any]]


class HealthResponse(BaseModel):
    status: str
    publication_enabled: bool
    publication_gate_issues: list[str]


def create_app(
    *,
    settings: Settings | None = None,
    schema_loader: SchemaLoader | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        runtime_settings = settings or get_settings()
        database_engine = engine or create_database_engine(runtime_settings.database_url)
        monday_client: MondayClient | None = None
        loader = schema_loader
        if loader is None:
            monday_client = MondayClient.from_settings(runtime_settings)
            loader = monday_client.load_sales_board_schema

        application.state.settings = runtime_settings
        application.state.database_engine = database_engine
        application.state.session_factory = create_session_factory(database_engine)
        application.state.publication_gate = await run_in_threadpool(
            partial(
                validate_schema_at_startup,
                loader,
                contract=runtime_settings.board_contract,
            )
        )
        try:
            yield
        finally:
            if monday_client is not None:
                monday_client.close()
            if engine is None:
                database_engine.dispose()

    application = FastAPI(
        title="Sales Board Automation",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        gate = request.app.state.publication_gate
        return HealthResponse(
            status="ok",
            publication_enabled=gate.publication_enabled,
            publication_gate_issues=[issue.code for issue in gate.result.issues],
        )

    return application


app = create_app()