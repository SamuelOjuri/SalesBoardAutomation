import pytest
from pydantic import ValidationError

from app.config import (
    BOARD_CONTRACT,
    DEFAULT_EXCLUDED_SALES_GROUP_IDS,
    Settings,
)


def settings_values() -> dict[str, object]:
    return {
        "database_url": "postgres://user:password@localhost/sales",
        "monday_ingestion_access_token": "token",
        "monday_webhook_shared_secret": "shared-secret",
        "gemini_api_key": "gemini-key",
        "gemini_model": "gemini-test-model",
    }


def test_settings_normalise_phase_one_configuration() -> None:
    values = settings_values()
    values.update(
        processing_mode="ALLOWLIST",
        processing_allowlist_item_ids="123, 456,123",
        processing_excluded_group_ids="group_mm5eqjq4, topics,group_mm5eqjq4",
        internal_email_domains="TAPEREDPLUS.CO.UK, www.Example.COM",
    )

    settings = Settings(_env_file=None, **values)

    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.monday_api_version == "2026-07"
    assert settings.processing_mode == "allowlist"
    assert settings.processing_allowlist_item_ids == ["123", "456"]
    assert settings.processing_excluded_group_ids == [
        "group_mm5eqjq4",
        "topics",
    ]
    assert settings.internal_email_domains == ["taperedplus.co.uk", "example.com"]
    assert settings.sales_board_id == BOARD_CONTRACT.sales_board_id
    assert settings.accounts_board_id == BOARD_CONTRACT.accounts_board_id


def test_completed_folder_is_excluded_by_default() -> None:
    settings = Settings(_env_file=None, **settings_values())

    assert settings.processing_excluded_group_ids == list(
        DEFAULT_EXCLUDED_SALES_GROUP_IDS
    )


def test_settings_require_a_webhook_authentication_method() -> None:
    values = settings_values()
    values["monday_webhook_shared_secret"] = "  "
    values["monday_signing_secret"] = None

    with pytest.raises(ValidationError, match="monday_signing_secret"):
        Settings(_env_file=None, **values)


def test_allowlist_mode_requires_at_least_one_item() -> None:
    values = settings_values()
    values["processing_mode"] = "allowlist"

    with pytest.raises(ValidationError, match="must not be empty"):
        Settings(_env_file=None, **values)


def test_runtime_board_ids_form_the_schema_contract() -> None:
    values = settings_values()
    values.update(sales_board_id=123, accounts_board_id=456)

    settings = Settings(_env_file=None, **values)

    assert settings.board_contract.sales_board_id == 123
    assert settings.board_contract.accounts_board_id == 456
