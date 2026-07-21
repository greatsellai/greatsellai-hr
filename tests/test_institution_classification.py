from __future__ import annotations

from app.services.institution_service import (
    classify_education_institution,
    load_higher_education_registry,
    load_registry,
)


def _classify(
    school_name_raw: str,
    *,
    degree: str = "bachelor",
    evidence_text: str | None = None,
):
    return classify_education_institution(
        school_name_raw=school_name_raw,
        degree=degree,
        evidence_text=evidence_text or school_name_raw,
        evidence_block_ids=["page-001"],
    )


def test_controlled_rosters_split_985_211_and_higher_education_levels() -> None:
    historical = load_registry()
    higher_education = load_higher_education_registry()

    assert len(historical.institutions) == 112
    assert sum(
        item.roster_id.startswith("cn-985-") for item in historical.institutions
    ) == 39
    assert len(higher_education.institutions) == 2952
    assert sum(
        item.classification == "undergraduate"
        for item in higher_education.institutions
    ) == 1412
    assert sum(
        item.classification == "associate"
        for item in higher_education.institutions
    ) == 1540

    assert _classify("\u5317\u4eac\u5927\u5b66").classification == "985"
    assert _classify("\u5317\u4eac\u5de5\u4e1a\u5927\u5b66").classification == "211"
    assert (
        _classify("\u5317\u4eac\u8bed\u8a00\u5927\u5b66").classification
        == "undergraduate"
    )
    assert (
        _classify("\u5317\u4eac\u5de5\u4e1a\u804c\u4e1a\u6280\u672f\u5b66\u9662").classification
        == "associate"
    )


def test_degree_wording_or_english_name_never_infers_a_school_type() -> None:
    assert (
        _classify(
            "\u672a\u77e5\u5b66\u9662",
            evidence_text="\u672c\u79d1\u6bd5\u4e1a",
        ).classification
        is None
    )
    assert (
        _classify(
            "Example University",
            degree="master",
            evidence_text="Example University Master of Science",
        ).classification
        is None
    )


def test_secondary_and_overseas_need_explicit_source_evidence() -> None:
    secondary = _classify(
        "\u793a\u4f8b\u804c\u4e1a\u9ad8\u4e2d",
        degree="high_school",
        evidence_text="\u793a\u4f8b\u804c\u4e1a\u9ad8\u4e2d \u6bd5\u4e1a",
    )
    assert secondary.classification == "secondary_vocational"
    assert secondary.basis == "source_evidence"

    assert (
        _classify(
            "\u793a\u4f8b\u9ad8\u4e2d",
            degree="high_school",
            evidence_text="\u793a\u4f8b\u9ad8\u4e2d \u6bd5\u4e1a",
        ).classification
        is None
    )

    overseas = _classify(
        "Example University",
        degree="master",
        evidence_text="\u7f8e\u56fd Example University \u7855\u58eb\u6bd5\u4e1a",
    )
    assert overseas.classification == "overseas"
    assert overseas.basis == "source_evidence"

    assert (
        _classify(
            "Example University",
            degree="master",
            evidence_text="\u7f8e\u56fd Example University \u4ea4\u6362\u5b66\u4e60",
        ).classification
        is None
    )
