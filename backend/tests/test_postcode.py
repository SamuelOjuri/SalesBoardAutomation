import pytest
from pydantic import ValidationError

from app.services.postcode import (
    DesignParameterExtraction,
    extract_parameters,
    extract_postcode_area,
    format_dropdown_for_monday,
    resolve_postcode_label,
)


def postcode_column() -> dict[str, object]:
    return {
        "id": "dropdown_mm60y5x8",
        "type": "dropdown",
        "settings": {
            "labels": [
                {"id": 114, "name": "W"},
                {"id": 115, "name": "WA"},
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


def test_model_output_rejects_unexpected_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DesignParameterExtraction.model_validate(
            {"post_code": "WA4 6NL", "item_id": "1953164969"}
        )


def test_live_dropdown_maps_wa_to_label_115() -> None:
    resolution = resolve_postcode_label("WA4 6NL", postcode_column())

    assert resolution is not None
    assert resolution.area == "WA"
    assert resolution.label_id == 115
    assert format_dropdown_for_monday("WA4 6NL", postcode_column()) == {
        "ids": [115]
    }


def test_unknown_or_duplicated_live_label_is_never_created_or_selected() -> None:
    column = postcode_column()
    labels = column["settings"]["labels"]  # type: ignore[index]
    labels.append({"id": 999, "name": "WA"})  # type: ignore[union-attr]

    assert resolve_postcode_label("WA4 6NL", column) is None
    assert format_dropdown_for_monday("ZZ1 1ZZ", postcode_column()) is None


def test_wrong_dropdown_column_is_not_used() -> None:
    column = postcode_column()
    column["id"] = "some_other_dropdown"

    assert resolve_postcode_label("WA4 6NL", column) is None