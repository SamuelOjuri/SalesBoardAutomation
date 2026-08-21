"""Operator commands for durable Sales item processing."""

from __future__ import annotations

import argparse
import json
import uuid
from typing import Any, Sequence

from app.config import get_settings
from app.database import create_database_engine, create_session_factory
from app.monday_client import MondayClient
from app.services.operations import (
    collect_processing_metrics,
    enqueue_sales_item,
    reconcile_sales_item,
    retry_failed_job,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Operate the Sales Board Automation processing queue."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    enqueue = commands.add_parser(
        "enqueue",
        help="Enqueue one authoritative Sales item.",
    )
    enqueue.add_argument("item_id")
    retry = commands.add_parser(
        "retry",
        help="Create a resumable successor for one failed job.",
    )
    retry.add_argument("job_id", type=uuid.UUID)
    reconcile = commands.add_parser(
        "reconcile",
        help="Reconcile one Sales item with authoritative Monday state.",
    )
    reconcile.add_argument("item_id")
    commands.add_parser(
        "metrics",
        help="Display queue, stage, item, and stale-lease metrics.",
    )
    return parser


def run_command(arguments: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    monday_client: MondayClient | None = None
    try:
        with session_factory() as session:
            if arguments.command == "metrics":
                return collect_processing_metrics(
                    session,
                    lease_timeout_seconds=settings.worker_lease_timeout_seconds,
                ).as_dict()
            if arguments.command == "retry":
                job = retry_failed_job(session, arguments.job_id)
                session.commit()
                return {
                    "outcome": "scheduled",
                    "jobId": str(job.id),
                    "itemId": job.item_id,
                    "stage": job.stage,
                }

            monday_client = MondayClient.from_settings(settings)
            if arguments.command == "enqueue":
                queued = enqueue_sales_item(
                    session,
                    monday_client,
                    arguments.item_id,
                    pipeline_version=settings.processing_pipeline_version,
                )
                session.commit()
                return {
                    "outcome": queued.outcome,
                    "jobId": str(queued.job.id),
                    "itemId": queued.item.item_id,
                    "inputRevision": queued.item.latest_input_revision,
                }
            if arguments.command == "reconcile":
                reconciled = reconcile_sales_item(
                    session,
                    monday_client,
                    arguments.item_id,
                    pipeline_version=settings.processing_pipeline_version,
                )
                session.commit()
                return {
                    "outcome": reconciled.outcome,
                    "jobId": (
                        str(reconciled.job_id)
                        if reconciled.job_id is not None
                        else None
                    ),
                    "itemId": reconciled.item_id,
                    "inputRevision": reconciled.input_revision,
                }
        raise RuntimeError("unsupported operator command")
    finally:
        if monday_client is not None:
            monday_client.close()
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    arguments = build_parser().parse_args(argv)
    print(json.dumps(run_command(arguments), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()