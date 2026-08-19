"""Deterministic identity for the authoritative set of input email assets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable


@dataclass(frozen=True, slots=True)
class EmailAssetIdentity:
    asset_id: str
    filename: str
    size_bytes: int
    created_at: datetime

    def __post_init__(self) -> None:
        asset_id = str(self.asset_id).strip()
        if not asset_id.isdecimal() or int(asset_id) <= 0:
            raise ValueError("asset_id must be a positive decimal ID")
        if not self.filename.strip() or "\x00" in self.filename:
            raise ValueError("filename must not be empty or contain a null byte")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must not be negative")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        object.__setattr__(self, "asset_id", asset_id)

    def as_manifest_entry(self) -> dict[str, str | int]:
        created_at = self.created_at.astimezone(timezone.utc)
        return {
            "asset_id": self.asset_id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "created_at": created_at.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
        }


def build_input_manifest(
    assets: Iterable[EmailAssetIdentity],
) -> list[dict[str, str | int]]:
    ordered_assets = sorted(assets, key=lambda asset: int(asset.asset_id))
    if not ordered_assets:
        raise ValueError("at least one input asset is required")
    asset_ids = [asset.asset_id for asset in ordered_assets]
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError("input assets must have unique asset IDs")
    return [asset.as_manifest_entry() for asset in ordered_assets]


def compute_input_revision(assets: Iterable[EmailAssetIdentity]) -> str:
    manifest = build_input_manifest(assets)
    canonical_json = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()