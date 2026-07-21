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
    registry_roster_id: str | None = None,
):
    return classify_education_institution(
        school_name_raw=school_name_raw,
        degree=degree,
        evidence_text=evidence_text or school_name_raw,
        evidence_block_ids=["page-001"],
        registry_roster_id=registry_roster_id,
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


def test_non_degree_study_never_inherits_the_host_school_classification() -> None:
    # Both records name a controlled 985 institution, but neither is a formal
    # degree record.  A whitelist must not turn a summer school into 985.
    assert (
        _classify(
            "\u6e05\u534e\u5927\u5b66",
            degree="unknown",
            evidence_text="\u6e05\u534e\u5927\u5b66\u6691\u671f\u5b66\u6821",
        ).classification
        is None
    )
    assert (
        _classify(
            "\u6e05\u534e\u5927\u5b66",
            degree="bachelor",
            evidence_text="\u6e05\u534e\u5927\u5b66 summer school",
        ).classification
        is None
    )


def test_registry_hint_cannot_override_the_source_grounded_school_name() -> None:
    roster_id = load_registry().institutions[0].roster_id
    assert (
        _classify(
            "Example University",
            evidence_text="Example University Bachelor of Science",
            registry_roster_id=roster_id,
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

    # A country word only inside an institution name is not sufficient.  It
    # could be a domestic cooperation programme rather than an overseas
    # degree-awarding institution.
    assert (
        _classify(
            "\u7f8e\u56fd\u793a\u4f8b\u5927\u5b66",
            degree="master",
            evidence_text="\u7f8e\u56fd\u793a\u4f8b\u5927\u5b66 \u7855\u58eb\u6bd5\u4e1a",
        ).classification
        is None
    )

    assert (
        _classify(
            "Example University",
            degree="master",
            evidence_text="\u7f8e\u56fd Example University \u4ea4\u6362\u5b66\u4e60",
        ).classification
        is None
    )


def test_source_markers_must_be_local_to_the_grounded_school() -> None:
    filler = " \u9879\u76ee\u7ecf\u5386" * 100
    # The page mentions an overseas market project far away from the domestic
    # school.  Page-level source evidence must not turn that school overseas.
    assert (
        _classify(
            "East China University of Science and Technology",
            degree="bachelor",
            evidence_text=(
                "East China University of Science and Technology \u672c\u79d1"
                f"{filler} \u7f8e\u56fd\u5e02\u573a\u9879\u76ee"
            ),
        ).classification
        is None
    )
    # Likewise, a secondary-vocational marker belonging to a different line
    # on the same page cannot classify an unrelated high school.
    assert (
        _classify(
            "\u793a\u4f8b\u9ad8\u4e2d",
            degree="high_school",
            evidence_text=(
                "\u793a\u4f8b\u9ad8\u4e2d\u6bd5\u4e1a"
                f"{filler} \u53e6\u4e00\u6240\u804c\u4e1a\u9ad8\u4e2d"
            ),
        ).classification
        is None
    )
