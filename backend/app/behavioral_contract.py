"""Phase 0 behavioral rules shared by later pipeline stages."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath


class AccountResolution(StrEnum):
    MATCHED = "matched"
    UNRESOLVED = "unresolved"


class ExistingValuePolicy(StrEnum):
    FILL_ONLY = "fill_only"


class PostcodeOutput(StrEnum):
    ALPHABETIC_AREA = "alphabetic_area"


@dataclass(frozen=True)
class BehavioralContract:
    supported_email_extensions: frozenset[str] = frozenset({".eml", ".msg"})
    postcode_output: PostcodeOutput = PostcodeOutput.ALPHABETIC_AREA
    duplicate_account_label_id: int = 1
    ambiguous_account_resolution: AccountResolution = AccountResolution.UNRESOLVED
    existing_value_policy: ExistingValuePolicy = ExistingValuePolicy.FILL_ONLY
    postcode_and_accounts_are_independent: bool = True
    email_content_is_trusted: bool = False
    model_output_is_trusted: bool = False

    def supports_email_file(self, filename: str) -> bool:
        return PurePath(filename).suffix.casefold() in self.supported_email_extensions


BEHAVIORAL_CONTRACT = BehavioralContract()