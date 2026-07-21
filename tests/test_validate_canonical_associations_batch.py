from tools.validate_canonical_associations_batch import (
    _build_batch_validation_query,
    _patient_cohort_values,
)


def test_patient_cohort_values_escapes_and_orders_ids():
    values = _patient_cohort_values(["P-0001", "P-00'02"])

    assert "('P-0001')" in values
    assert "('P-00\\'02')" in values


def test_build_batch_validation_query_references_expected_sources():
    sql = _build_batch_validation_query(["P-0001", "P-0002"])

    assert "WITH patient_cohort(patient_id) AS" in sql
    assert "FROM cdsi_prod.pathology_data_mining.canonical_slide_associations" in sql
    assert "FROM cdsi_eng_phi.pdm_base_tables_dev.impact_block_matched_slides_v1" in sql
    assert "FROM cdsi_eng_phi.pdm_base_tables.impact_matched_slides" in sql
    assert "FROM cdsi_eng_phi.pdm_base_tables_dev.case_breakdown_cleaned_v2" in sql
    assert "FULL OUTER JOIN legacy_indexed" in sql
    assert "missing_from_canonical_count" in sql
    assert "extra_in_canonical_count" in sql


def test_build_batch_validation_query_counts_only_omits_diff_counts():
    sql = _build_batch_validation_query(["P-0001"], counts_only=True)

    assert "FULL OUTER JOIN legacy_indexed" not in sql
    assert "missing_from_canonical_count" not in sql
    assert "extra_in_canonical_count" not in sql
    assert "canonical_count" in sql
    assert "legacy_count" in sql
