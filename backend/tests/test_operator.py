import json
import uuid
from types import SimpleNamespace

import pytest

from app import operator
from app.config import BOARD_CONTRACT
from app.database import Base, create_database_engine, create_session_factory
from app.models import ProcessingAudit, ProcessingJob


@pytest.mark.parametrize("command", ["enqueue", "reconcile"])
@pytest.mark.parametrize("force_reprocess", [False, True])
def test_operator_passes_force_reprocess_to_durable_queue(
    tmp_path, monkeypatch, command, force_reprocess,
) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'operator.db').as_posix()}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(operator, "get_settings", lambda: SimpleNamespace(
        database_url=database_url,
        processing_pipeline_version="test-v1",
        processing_excluded_group_ids=[],
    ))

    class Monday:
        closed = False

        def load_sales_item_intake(self, item_id):
            return {
                "id": item_id,
                "state": "active",
                "board": {"id": str(BOARD_CONTRACT.sales_board_id)},
                "group": {"id": "topics"},
                "assets": [{
                    "id": "10", "name": "request.eml", "file_size": 12,
                    "created_at": "2026-08-21T10:00:00Z",
                    "public_url": "https://files.example/request.eml",
                }],
                "column_values": [{
                    "id": BOARD_CONTRACT.email_file_column_id,
                    "type": "file",
                    "value": json.dumps({"files": [{"assetId": "10"}]}),
                }],
            }

        def close(self):
            self.closed = True

    monday = Monday()
    monkeypatch.setattr(operator.MondayClient, "from_settings", lambda _: monday)
    arguments = [command, "42"] + (["--force-reprocess"] if force_reprocess else [])
    try:
        result = operator.run_command(operator.build_parser().parse_args(arguments))
        with create_session_factory(engine)() as session:
            job = session.get(ProcessingJob, uuid.UUID(result["jobId"]))
            assert job is not None
            assert job.result_json == ({"forceReprocess": True} if force_reprocess else None)
            audit = session.query(ProcessingAudit).filter_by(job_id=job.id).one()
            assert audit.details_json.get("forceReprocess", False) is force_reprocess
        assert result["outcome"] == "queued"
        assert monday.closed
    finally:
        engine.dispose()
