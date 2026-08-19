from datetime import datetime, timedelta, timezone

import pytest

from app.input_revision import EmailAssetIdentity, build_input_manifest, compute_input_revision


def asset(asset_id: str, *, size_bytes: int = 100) -> EmailAssetIdentity:
    return EmailAssetIdentity(
        asset_id=asset_id,
        filename=f"request-{asset_id}.eml",
        size_bytes=size_bytes,
        created_at=datetime(2026, 8, 19, 9, 30, tzinfo=timezone.utc),
    )


def test_input_revision_is_stable_and_uses_numeric_asset_order() -> None:
    assets = [asset("10"), asset("2")]

    assert compute_input_revision(assets) == compute_input_revision(reversed(assets))
    assert [entry["asset_id"] for entry in build_input_manifest(assets)] == ["2", "10"]


def test_each_required_asset_field_changes_the_revision() -> None:
    original = asset("1")
    variants = [
        asset("2"),
        EmailAssetIdentity(
            "1", "renamed.eml", original.size_bytes, original.created_at
        ),
        asset("1", size_bytes=101),
        EmailAssetIdentity(
            "1",
            original.filename,
            original.size_bytes,
            original.created_at + timedelta(seconds=1),
        ),
    ]

    assert all(
        compute_input_revision([variant]) != compute_input_revision([original])
        for variant in variants
    )


def test_input_revision_rejects_missing_or_ambiguous_assets() -> None:
    with pytest.raises(ValueError, match="at least one"):
        compute_input_revision([])
    with pytest.raises(ValueError, match="unique"):
        compute_input_revision([asset("1"), asset("1")])
    with pytest.raises(ValueError, match="timezone"):
        EmailAssetIdentity("1", "request.eml", 10, datetime(2026, 8, 19))