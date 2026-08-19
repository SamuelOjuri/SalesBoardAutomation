from app.behavioral_contract import (
    BEHAVIORAL_CONTRACT,
    AccountResolution,
    ExistingValuePolicy,
    PostcodeOutput,
)
from app.config import REQUIRED_POSTCODE_LABELS, REFERENCE_IMPLEMENTATION_COMMIT


def test_only_msg_and_eml_files_are_supported() -> None:
    assert BEHAVIORAL_CONTRACT.supports_email_file("request.msg")
    assert BEHAVIORAL_CONTRACT.supports_email_file("REQUEST.EML")
    assert not BEHAVIORAL_CONTRACT.supports_email_file("request.pdf")
    assert not BEHAVIORAL_CONTRACT.supports_email_file("request.msg.exe")


def test_publication_and_matching_rules_are_frozen() -> None:
    assert BEHAVIORAL_CONTRACT.postcode_output is PostcodeOutput.ALPHABETIC_AREA
    assert BEHAVIORAL_CONTRACT.duplicate_account_label_id == 1
    assert (
        BEHAVIORAL_CONTRACT.ambiguous_account_resolution
        is AccountResolution.UNRESOLVED
    )
    assert BEHAVIORAL_CONTRACT.existing_value_policy is ExistingValuePolicy.FILL_ONLY
    assert BEHAVIORAL_CONTRACT.postcode_and_accounts_are_independent


def test_email_and_model_data_are_explicitly_untrusted() -> None:
    assert not BEHAVIORAL_CONTRACT.email_content_is_trusted
    assert not BEHAVIORAL_CONTRACT.model_output_is_trusted


def test_reference_commit_and_complete_postcode_map_are_pinned() -> None:
    assert REFERENCE_IMPLEMENTATION_COMMIT == (
        "ef321095ed96a7dde6543b89da58b2689e76a53d"
    )
    assert len(REQUIRED_POSTCODE_LABELS) == 123
    assert (115, "WA") in {
        (label.id, label.name) for label in REQUIRED_POSTCODE_LABELS
    }