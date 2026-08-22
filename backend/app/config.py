"""Immutable board contracts and validated runtime configuration."""

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


REFERENCE_IMPLEMENTATION_COMMIT = "ef321095ed96a7dde6543b89da58b2689e76a53d"
PROCESSING_PIPELINE_RELEASE = "sales-requester-v1"
POSTCODE_EXTRACTION_REVISION = "postcode-extraction-v4"
POSTCODE_NORMALIZATION_REVISION = "postcode-area-v1"
POSTCODE_LABEL_MAPPING_REVISION = "postcode-label-mapping-v1"
REQUESTER_IDENTITY_REVISION = "requester-identity-v3"
ACCOUNT_MATCHING_REVISION = "account-matching-v3"
DEFAULT_EXCLUDED_SALES_GROUP_IDS = ("group_mm5eqjq4",)
DEFAULT_INTERNAL_COMPANY_ALIASES = (
    "TaperedPlus",
    "Tapered Plus",
    "Tapered Plus Limited",
)
DEFAULT_REQUESTER_DOMAIN_ALIASES = {
    "tremcocpgsupport.zendesk.com": "tremcocpg.com",
}
DEFAULT_ACCOUNT_REQUESTER_DOMAIN_ALIASES = {
    "1661824807": ("sigplc.com",),
}


def build_processing_pipeline_version(
    gemini_model: str,
    *,
    extraction_revision: str = POSTCODE_EXTRACTION_REVISION,
    normalization_revision: str = POSTCODE_NORMALIZATION_REVISION,
    label_mapping_revision: str = POSTCODE_LABEL_MAPPING_REVISION,
    requester_identity_revision: str = REQUESTER_IDENTITY_REVISION,
    account_matching_revision: str = ACCOUNT_MATCHING_REVISION,
) -> str:
    """Build the immutable identity of every result-affecting pipeline part."""

    components = {
        "gemini": gemini_model,
        "extraction": extraction_revision,
        "normalization": normalization_revision,
        "mapping": label_mapping_revision,
        "requester": requester_identity_revision,
        "matching": account_matching_revision,
    }
    normalized = {
        name: str(value).strip() for name, value in components.items()
    }
    if any(not value for value in normalized.values()):
        raise ValueError("pipeline version components must not be empty")
    component_version = "|".join(
        f"{name}={value}" for name, value in normalized.items()
    )
    return f"{PROCESSING_PIPELINE_RELEASE}|{component_version}"


@dataclass(frozen=True)
class PostcodeLabelContract:
    id: int
    name: str


REQUIRED_POSTCODE_LABELS = tuple(
    PostcodeLabelContract(id=label_id, name=name)
    for label_id, name in (
        (1, "AB"),
        (2, "AL"),
        (3, "B"),
        (4, "BA"),
        (5, "BB"),
        (6, "BD"),
        (7, "BF"),
        (8, "BH"),
        (9, "BL"),
        (10, "BN"),
        (11, "BR"),
        (12, "BS"),
        (13, "BT"),
        (14, "CA"),
        (15, "CB"),
        (16, "CF"),
        (17, "CH"),
        (18, "CM"),
        (20, "CO"),
        (21, "CR"),
        (22, "CT"),
        (23, "CV"),
        (24, "CW"),
        (25, "DA"),
        (26, "DD"),
        (27, "DE"),
        (28, "DG"),
        (29, "DH"),
        (30, "DL"),
        (31, "DN"),
        (32, "DT"),
        (33, "DY"),
        (34, "E"),
        (35, "EC"),
        (36, "EH"),
        (37, "EN"),
        (38, "EX"),
        (39, "FK"),
        (40, "FY"),
        (41, "G"),
        (42, "GL"),
        (43, "GU"),
        (44, "GY"),
        (45, "HA"),
        (46, "HD"),
        (47, "HG"),
        (48, "HP"),
        (49, "HR"),
        (50, "HS"),
        (51, "HU"),
        (52, "HX"),
        (53, "IG"),
        (54, "IP"),
        (55, "IV"),
        (56, "KA"),
        (57, "KT"),
        (58, "KW"),
        (59, "KY"),
        (60, "L"),
        (61, "LA"),
        (62, "LD"),
        (63, "LE"),
        (64, "LL"),
        (65, "LN"),
        (66, "LS"),
        (67, "LU"),
        (68, "M"),
        (69, "ME"),
        (70, "MK"),
        (71, "ML"),
        (72, "N"),
        (73, "NE"),
        (74, "NG"),
        (75, "NN"),
        (76, "NP"),
        (77, "NR"),
        (78, "NW"),
        (79, "OL"),
        (80, "OX"),
        (81, "PA"),
        (82, "PE"),
        (83, "PH"),
        (84, "PL"),
        (85, "PO"),
        (86, "PR"),
        (87, "RG"),
        (88, "RH"),
        (89, "RM"),
        (90, "S"),
        (91, "SA"),
        (92, "SE"),
        (93, "SG"),
        (94, "SK"),
        (95, "SL"),
        (96, "SM"),
        (97, "SN"),
        (98, "SO"),
        (99, "SP"),
        (100, "SR"),
        (101, "SS"),
        (102, "ST"),
        (103, "SW"),
        (104, "SY"),
        (105, "TA"),
        (106, "TD"),
        (107, "TF"),
        (108, "TN"),
        (109, "TQ"),
        (110, "TR"),
        (111, "TS"),
        (112, "TW"),
        (113, "UB"),
        (114, "W"),
        (115, "WA"),
        (116, "WC"),
        (117, "WD"),
        (119, "WF"),
        (120, "WN"),
        (121, "WR"),
        (122, "WS"),
        (123, "WV"),
        (124, "YO"),
        (125, "ZE"),
    )
)


@dataclass(frozen=True)
class BoardContract:
    sales_board_id: int = 5_100_711_564
    accounts_board_id: int = 1_654_217_230
    email_file_column_id: str = "file_mm5erpbb"
    accounts_relation_column_id: str = "board_relation_mm64107r"
    postcode_column_id: str = "dropdown_mm60y5x8"
    account_email_domain_column_id: str = "text_mm6bymv5"
    account_duplicate_column_id: str = "dropdown_mm6cxq2p"
    required_postcode_labels: tuple[
        PostcodeLabelContract, ...
    ] = REQUIRED_POSTCODE_LABELS


BOARD_CONTRACT = BoardContract()


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or a local .env file."""

    database_url: str
    monday_ingestion_access_token: SecretStr
    monday_signing_secret: SecretStr | None = None
    monday_webhook_shared_secret: SecretStr | None = None
    monday_api_url: str = "https://api.monday.com/v2"
    monday_api_version: str = "2026-07"
    monday_request_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    monday_request_max_attempts: int = Field(default=3, ge=1, le=10)

    gemini_api_key: SecretStr
    gemini_model: str
    processing_pipeline_version: str = ""
    processing_mode: Literal["off", "shadow", "allowlist", "enabled"] = "off"
    processing_allowlist_item_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    processing_excluded_group_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDED_SALES_GROUP_IDS)
    )
    worker_poll_interval_seconds: float = Field(default=2.0, gt=0, le=60)
    worker_heartbeat_interval_seconds: float = Field(default=30.0, gt=0, le=300)
    worker_lease_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    worker_retry_base_seconds: float = Field(default=30.0, gt=0, le=3600)
    worker_retry_max_seconds: float = Field(default=900.0, gt=0, le=86400)
    internal_email_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["taperedplus.co.uk"]
    )
    internal_company_aliases: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_INTERNAL_COMPANY_ALIASES)
    )
    requester_domain_aliases: Annotated[dict[str, str], NoDecode] = Field(
        default_factory=lambda: dict(DEFAULT_REQUESTER_DOMAIN_ALIASES)
    )
    account_requester_domain_aliases: Annotated[
        dict[str, list[str]], NoDecode
    ] = Field(
        default_factory=lambda: {
            item_id: list(domains)
            for item_id, domains in DEFAULT_ACCOUNT_REQUESTER_DOMAIN_ALIASES.items()
        }
    )

    sales_board_id: int = Field(default=BOARD_CONTRACT.sales_board_id, gt=0)
    accounts_board_id: int = Field(default=BOARD_CONTRACT.accounts_board_id, gt=0)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalise_database_url(cls, value: object) -> str:
        url = str(value).strip()
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+psycopg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg://", 1)
        if not url.startswith("postgresql+psycopg://"):
            raise ValueError("database_url must use PostgreSQL with psycopg")
        return url

    @field_validator("monday_ingestion_access_token", "gemini_api_key", mode="before")
    @classmethod
    def _require_nonempty_secret(cls, value: object) -> str:
        secret = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        if not secret.strip():
            raise ValueError("secret must not be empty")
        return secret.strip()

    @field_validator(
        "monday_signing_secret", "monday_webhook_shared_secret", mode="before"
    )
    @classmethod
    def _normalise_optional_secret(cls, value: object) -> object:
        if value is None:
            return None
        secret = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        return secret.strip() or None

    @field_validator("monday_api_url", "gemini_model", mode="before")
    @classmethod
    def _require_nonempty_value(cls, value: object) -> str:
        normalised = str(value).strip()
        if not normalised:
            raise ValueError("value must not be empty")
        return normalised.rstrip("/")

    @field_validator("monday_api_version", mode="before")
    @classmethod
    def _validate_monday_api_version(cls, value: object) -> str:
        normalised = str(value).strip()
        year, separator, month = normalised.partition("-")
        if separator != "-" or len(year) != 4 or len(month) != 2:
            raise ValueError("monday_api_version must use YYYY-MM format")
        if not year.isdecimal() or not month.isdecimal() or not 1 <= int(month) <= 12:
            raise ValueError("monday_api_version must use YYYY-MM format")
        return normalised

    @field_validator("processing_mode", mode="before")
    @classmethod
    def _normalise_processing_mode(cls, value: object) -> str:
        return str(value).strip().lower()

    @field_validator("processing_allowlist_item_ids", mode="before")
    @classmethod
    def _normalise_allowlist(cls, value: object) -> list[str]:
        values = _parse_list_setting(value, "processing_allowlist_item_ids")
        normalised: list[str] = []
        for item_id in values:
            candidate = str(item_id).strip()
            if not candidate.isdecimal() or int(candidate) <= 0:
                raise ValueError(
                    "processing_allowlist_item_ids must contain positive decimal IDs"
                )
            if candidate not in normalised:
                normalised.append(candidate)
        return normalised

    @field_validator("processing_excluded_group_ids", mode="before")
    @classmethod
    def _normalise_excluded_group_ids(cls, value: object) -> list[str]:
        values = _parse_list_setting(value, "processing_excluded_group_ids")
        normalised: list[str] = []
        for group_id in values:
            candidate = str(group_id).strip()
            if not candidate:
                raise ValueError(
                    "processing_excluded_group_ids must contain non-empty IDs"
                )
            if candidate not in normalised:
                normalised.append(candidate)
        return normalised

    @field_validator("internal_email_domains", mode="before")
    @classmethod
    def _normalise_internal_domains(cls, value: object) -> list[str]:
        values = _parse_list_setting(value, "internal_email_domains")
        normalised: list[str] = []
        for domain in values:
            candidate = str(domain).strip().lower().removeprefix("www.").rstrip(".")
            if not candidate or "." not in candidate or "@" in candidate:
                raise ValueError("internal_email_domains contains an invalid domain")
            try:
                candidate = candidate.encode("idna").decode("ascii")
            except UnicodeError as error:
                raise ValueError(
                    "internal_email_domains contains an invalid domain"
                ) from error
            if candidate not in normalised:
                normalised.append(candidate)
        if not normalised:
            raise ValueError("internal_email_domains must not be empty")
        return normalised

    @field_validator("internal_company_aliases", mode="before")
    @classmethod
    def _normalise_internal_company_aliases(cls, value: object) -> list[str]:
        values = _parse_list_setting(value, "internal_company_aliases")
        normalised: list[str] = []
        for alias in values:
            candidate = " ".join(str(alias).split())
            if not candidate:
                raise ValueError(
                    "internal_company_aliases must contain non-empty values"
                )
            if candidate.casefold() not in {
                existing.casefold() for existing in normalised
            }:
                normalised.append(candidate)
        if not normalised:
            raise ValueError("internal_company_aliases must not be empty")
        return normalised

    @field_validator("requester_domain_aliases", mode="before")
    @classmethod
    def _normalise_requester_domain_aliases(
        cls, value: object
    ) -> dict[str, str]:
        mapping = _parse_mapping_setting(value, "requester_domain_aliases")
        normalised: dict[str, str] = {}
        for raw_source, raw_target in mapping.items():
            source = _normalise_domain_setting(raw_source)
            target = _normalise_domain_setting(raw_target)
            normalised[source] = target
        return normalised

    @field_validator("account_requester_domain_aliases", mode="before")
    @classmethod
    def _normalise_account_requester_domain_aliases(
        cls, value: object
    ) -> dict[str, list[str]]:
        mapping = _parse_mapping_setting(
            value,
            "account_requester_domain_aliases",
        )
        normalised: dict[str, list[str]] = {}
        for raw_item_id, raw_domains in mapping.items():
            item_id = str(raw_item_id).strip()
            if not item_id.isdecimal() or int(item_id) <= 0:
                raise ValueError(
                    "account_requester_domain_aliases keys must be positive "
                    "decimal Account IDs"
                )
            domains = _parse_list_setting(
                raw_domains,
                "account_requester_domain_aliases",
            )
            parsed_domains: list[str] = []
            for raw_domain in domains:
                domain = _normalise_domain_setting(raw_domain)
                if domain not in parsed_domains:
                    parsed_domains.append(domain)
            if not parsed_domains:
                raise ValueError(
                    "account_requester_domain_aliases values must not be empty"
                )
            normalised[item_id] = parsed_domains
        return normalised

    @model_validator(mode="after")
    def _validate_authentication_and_mode(self) -> "Settings":
        expected_pipeline_version = build_processing_pipeline_version(
            self.gemini_model
        )
        configured_pipeline_version = self.processing_pipeline_version.strip()
        if (
            configured_pipeline_version
            and configured_pipeline_version != expected_pipeline_version
        ):
            raise ValueError(
                "processing_pipeline_version must match the pinned model and "
                f"behavior revisions: {expected_pipeline_version}"
            )
        self.processing_pipeline_version = expected_pipeline_version
        if self.monday_signing_secret is None and self.monday_webhook_shared_secret is None:
            raise ValueError(
                "monday_signing_secret or monday_webhook_shared_secret is required"
            )
        if self.processing_mode == "allowlist" and not self.processing_allowlist_item_ids:
            raise ValueError(
                "processing_allowlist_item_ids must not be empty in allowlist mode"
            )
        if self.worker_heartbeat_interval_seconds >= self.worker_lease_timeout_seconds:
            raise ValueError(
                "worker heartbeat interval must be shorter than the lease timeout"
            )
        if self.worker_retry_base_seconds > self.worker_retry_max_seconds:
            raise ValueError(
                "worker retry base must not exceed the maximum retry delay"
            )
        return self

    @property
    def board_contract(self) -> BoardContract:
        return BoardContract(
            sales_board_id=self.sales_board_id,
            accounts_board_id=self.accounts_board_id,
        )


def _parse_list_setting(value: object, field_name: str) -> list[object]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        raw_value = value.strip()
        if not raw_value:
            return []
        if raw_value.startswith("["):
            try:
                parsed = json.loads(raw_value)
            except json.JSONDecodeError as error:
                raise ValueError(f"{field_name} must be CSV or a JSON array") from error
            if not isinstance(parsed, list):
                raise ValueError(f"{field_name} must be CSV or a JSON array")
            return parsed
        return raw_value.split(",")
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _parse_mapping_setting(
    value: object,
    field_name: str,
) -> dict[object, object]:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"{field_name} must be a JSON object") from error
    else:
        parsed = value
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return dict(parsed)


def _normalise_domain_setting(value: object) -> str:
    candidate = str(value).strip().casefold().removeprefix("www.").rstrip(".")
    if (
        not candidate
        or "." not in candidate
        or "@" in candidate
        or any(character.isspace() for character in candidate)
    ):
        raise ValueError("domain alias settings contain an invalid domain")
    try:
        return candidate.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("domain alias settings contain an invalid domain") from error


@lru_cache
def get_settings() -> Settings:
    return Settings()
