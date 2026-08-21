"""Background worker process for durable Sales item processing."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, cast

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.database import create_database_engine, create_session_factory
from app.monday_client import MondayClient
from app.publication_gate import validate_schema_at_startup
from app.services.accounts import AccountsIndexService
from app.services.pipeline import (
    PipelineDependencies,
    PipelineExecutionDisabled,
    ProcessingMode,
    run_pipeline_job,
)
from app.services.postcode import GeminiPostcodeClient
from app.services.worker import (
    JobLeaseError,
    claim_next_job,
    heartbeat_job,
    recover_stale_jobs,
    retry_or_fail_job,
    utc_now,
)


logger = logging.getLogger(__name__)
PipelineRunner = Callable[..., object]


@dataclass(slots=True)
class WorkerRuntime:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    monday_client: MondayClient
    dependencies: PipelineDependencies
    worker_id: str


class LeaseHeartbeat:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        job_id: uuid.UUID,
        *,
        worker_id: str,
        interval_seconds: float,
    ) -> None:
        self._session_factory = session_factory
        self._job_id = job_id
        self._worker_id = worker_id
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"job-heartbeat-{job_id}",
            daemon=True,
        )

    def __enter__(self) -> LeaseHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval_seconds + 1)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                with self._session_factory() as session:
                    heartbeat_job(
                        session,
                        self._job_id,
                        worker_id=self._worker_id,
                    )
                    session.commit()
            except JobLeaseError:
                return
            except Exception as error:
                logger.warning(
                    "processing job heartbeat failed",
                    extra={
                        "job_id": str(self._job_id),
                        "error_type": type(error).__name__,
                    },
                )


@contextmanager
def create_worker_runtime(
    settings: Settings | None = None,
    *,
    worker_id: str | None = None,
) -> Iterator[WorkerRuntime]:
    runtime_settings = settings or get_settings()
    engine = create_database_engine(runtime_settings.database_url)
    session_factory = create_session_factory(engine)
    monday_client = MondayClient.from_settings(runtime_settings)
    gate = validate_schema_at_startup(
        monday_client.load_sales_board_schema,
        contract=runtime_settings.board_contract,
    )
    accounts = AccountsIndexService(
        client=monday_client,
        board_id=runtime_settings.accounts_board_id,
    )
    dependencies = PipelineDependencies(
        monday=monday_client,
        postcode_client=GeminiPostcodeClient.from_settings(runtime_settings),
        accounts=accounts,
        publication_gate=gate,
        internal_email_domains=tuple(runtime_settings.internal_email_domains),
    )
    runtime = WorkerRuntime(
        settings=runtime_settings,
        engine=engine,
        session_factory=session_factory,
        monday_client=monday_client,
        dependencies=dependencies,
        worker_id=worker_id or default_worker_id(),
    )
    try:
        yield runtime
    finally:
        monday_client.close()
        engine.dispose()


def process_next_job(
    runtime: WorkerRuntime,
    *,
    pipeline_runner: PipelineRunner = run_pipeline_job,
    now: datetime | None = None,
) -> bool:
    processing_time = now or utc_now()
    settings = runtime.settings
    mode = cast(ProcessingMode, settings.processing_mode)
    with runtime.session_factory() as session:
        recovery = recover_stale_jobs(
            session,
            lease_timeout_seconds=settings.worker_lease_timeout_seconds,
            now=processing_time,
            retry_base_seconds=settings.worker_retry_base_seconds,
            retry_max_seconds=settings.worker_retry_max_seconds,
        )
        if recovery.retried or recovery.failed:
            logger.info(
                "recovered stale processing jobs",
                extra={
                    "retried": recovery.retried,
                    "failed": recovery.failed,
                },
            )
        if mode == "off":
            session.commit()
            return False
        claim_item_ids = (
            settings.processing_allowlist_item_ids
            if mode == "allowlist"
            else None
        )
        job = claim_next_job(
            session,
            worker_id=runtime.worker_id,
            item_ids=claim_item_ids,
            now=processing_time,
        )
        session.commit()

    if job is None:
        return False

    logger.info(
        "claimed processing job",
        extra={
            "job_id": str(job.id),
            "item_id": job.item_id,
            "stage": job.stage,
            "attempt_count": job.attempt_count,
        },
    )
    try:
        with LeaseHeartbeat(
            runtime.session_factory,
            job.id,
            worker_id=runtime.worker_id,
            interval_seconds=settings.worker_heartbeat_interval_seconds,
        ):
            outcome = pipeline_runner(
                runtime.session_factory,
                job.id,
                worker_id=runtime.worker_id,
                dependencies=runtime.dependencies,
                mode=mode,
                allowlist_item_ids=settings.processing_allowlist_item_ids,
            )
        logger.info(
            "finished processing job",
            extra={
                "job_id": str(job.id),
                "item_id": job.item_id,
                "outcome": str(outcome),
            },
        )
    except Exception as error:
        logger.error(
            "processing job failed",
            extra={
                "job_id": str(job.id),
                "item_id": job.item_id,
                "error_type": type(error).__name__,
            },
        )
        try:
            with runtime.session_factory() as session:
                retry_or_fail_job(
                    session,
                    job.id,
                    worker_id=runtime.worker_id,
                    error=error,
                    retryable=not isinstance(
                        error,
                        (PipelineExecutionDisabled, ValueError),
                    ),
                    retry_base_seconds=settings.worker_retry_base_seconds,
                    retry_max_seconds=settings.worker_retry_max_seconds,
                )
                session.commit()
        except JobLeaseError:
            logger.warning(
                "processing job failure could not update a lost lease",
                extra={"job_id": str(job.id)},
            )
    return True


def run_worker(
    runtime: WorkerRuntime,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    stop = stop_event or threading.Event()
    logger.info(
        "background worker started",
        extra={
            "worker_id": runtime.worker_id,
            "processing_mode": runtime.settings.processing_mode,
        },
    )
    while not stop.is_set():
        processed = process_next_job(runtime)
        if not processed:
            stop.wait(runtime.settings.worker_poll_interval_seconds)
    logger.info("background worker stopped", extra={"worker_id": runtime.worker_id})


def default_worker_id() -> str:
    instance = (
        os.getenv("RENDER_INSTANCE_ID")
        or os.getenv("HOSTNAME")
        or socket.gethostname()
    )
    return f"{instance}:{os.getpid()}"[:255]


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    with create_worker_runtime() as runtime:
        run_worker(runtime, stop_event=stop)


if __name__ == "__main__":
    main()