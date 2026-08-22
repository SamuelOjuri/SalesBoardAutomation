import pytest
from pydantic import ValidationError

from app.services.postcode import (
    DesignParameterExtraction,
    extract_parameters,
    extract_postcode_area,
    extract_structured_project_area,
    format_dropdown_for_monday,
    resolve_postcode_label,
    validate_company_evidence,
)


def postcode_column() -> dict[str, object]:
    return {
        "id": "dropdown_mm60y5x8",
        "type": "dropdown",
        "settings": {
            "labels": [
                {"id": 114, "label": "W", "is_deactivated": False},
                {"id": 115, "label": "WA", "is_deactivated": False},
            ]
        },
    }


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("WA4 6NL", "WA"),
        ("wa4 6nl", "WA"),
        ("wa46nl", "WA"),
        ("WA", "WA"),
        ("Of Project Location: WA4 6NL", "WA"),
        (None, None),
        ("Not provided", None),
        ("not a postcode", None),
    ],
)
def test_extract_postcode_area_preserves_reference_behaviour(
    raw_value: object, expected: str | None
) -> None:
    assert extract_postcode_area(raw_value) == expected


def test_extract_parameters_uses_strict_model_result() -> None:
    extracted = DesignParameterExtraction.model_validate(
        {"post_code": "wa46nl", "company": "Kingsgate Construction"}
    )

    assert extract_parameters("untrusted email", extracted_parameters=extracted) == {
        "Post Code": "WA"
    }


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("board_id", 5_100_711_564),
        ("item_id", "1953164969"),
        ("column_id", "board_relation_mm64107r"),
        ("mutation_payload", {"item_ids": [1953164969]}),
    ],
)
def test_model_output_cannot_supply_monday_control_fields(
    field_name: str,
    field_value: object,
) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DesignParameterExtraction.model_validate(
            {
                "post_code": "WA4 6NL",
                "company": "Example Roofing",
                field_name: field_value,
            }
        )


def test_live_dropdown_maps_wa_to_label_115() -> None:
    resolution = resolve_postcode_label("WA4 6NL", postcode_column())

    assert resolution is not None
    assert resolution.area == "WA"
    assert resolution.label_id == 115
    assert format_dropdown_for_monday("WA4 6NL", postcode_column()) == {
        "ids": [115]
    }


@pytest.mark.parametrize(
    "content",
    [
        "Project: Luton Sixth Form College, LU",
        "PROJECT DETAILS\nLuton Sixth Form College, LU\nReference: TP18491",
        "Project:\nLuton Sixth Form College,\nLU\nReference: TP18491",
    ],
)
def test_structured_project_area_accepts_only_explicit_project_suffix(
    content: str,
) -> None:
    assert extract_structured_project_area(content) == "LU"


@pytest.mark.parametrize(
    "content",
    [
        "Subject: Revision Request - Luton College",
        "The project is at Luton Sixth Form College.",
        "Filename: Luton Sixth Form College_ LU.pdf",
        "Project: Luton Sixth Form College",
        "Project address: Luton Sixth Form College, LU",
        "Project: First College, LU\nProject: Second College, HP",
    ],
)
def test_structured_project_area_rejects_inference_and_ambiguity(
    content: str,
) -> None:
    assert extract_structured_project_area(content) is None


def test_conflicting_model_and_structured_project_areas_fail_closed() -> None:
    extracted = DesignParameterExtraction(post_code="WA4 6NL", company=None)

    assert extract_parameters(
        "Project: Luton Sixth Form College, LU",
        extracted_parameters=extracted,
    ) == {"Post Code": "Not provided"}


def test_legacy_name_dropdown_label_remains_supported() -> None:
    column = postcode_column()
    labels = column["settings"]["labels"]  # type: ignore[index]
    for label in labels:  # type: ignore[union-attr]
        label["name"] = label.pop("label")

    resolution = resolve_postcode_label("WA4 6NL", column)

    assert resolution is not None
    assert resolution.label_id == 115


def test_unknown_or_duplicated_live_label_is_never_created_or_selected() -> None:
    column = postcode_column()
    labels = column["settings"]["labels"]  # type: ignore[index]
    labels.append(  # type: ignore[union-attr]
        {"id": 999, "label": "WA", "is_deactivated": False}
    )

    assert resolve_postcode_label("WA4 6NL", column) is None
    assert format_dropdown_for_monday("ZZ1 1ZZ", postcode_column()) is None


def test_wrong_dropdown_column_is_not_used() -> None:
    column = postcode_column()
    column["id"] = "some_other_dropdown"

    assert resolve_postcode_label("WA4 6NL", column) is None


def test_company_evidence_rejects_internal_alias_and_domain_only_mentions() -> None:
    evidence = (
        "From: sales@taperedplus.co.uk\n"
        "Visit https://www.accuroof.co.uk/quote\n"
        "External requester: Styrene Packaging"
    )

    assert (
        validate_company_evidence(
            "TaperedPlus",
            evidence=evidence,
            internal_company_aliases=("TaperedPlus", "Tapered Plus"),
        )
        is None
    )
    assert validate_company_evidence("AccuRoof", evidence=evidence) is None
    assert (
        validate_company_evidence("Styrene Packaging", evidence=evidence)
        == "Styrene Packaging"
    )


def test_company_evidence_requires_whole_consecutive_tokens() -> None:
    assert validate_company_evidence("SPI", evidence="Hospital project") is None
    assert validate_company_evidence("SPI", evidence="Company: SPI") == "SPI"
