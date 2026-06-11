"""
Single source of truth for Databricks table names and warehouse defaults.

Imported by both app/meta.py and tools/generate_resource_patient.py so that
changes to table names or the warehouse ID only need to be made here.
"""

#: De-identified slide ↔ clinical join table (PHI-restricted via Unity Catalog)
DEID_TABLE = "cdsi_prod.pathology_data_mining.impact_matched_slides_deid"

#: Slide file inventory — contains s3:// paths for each image_id
INVENTORY_TABLE = "cdsi_eng_phi.pdm_base_tables.slide_inventory"

#: Default Databricks SQL warehouse (can be overridden via DATABRICKS_WAREHOUSE_ID)
DEFAULT_WAREHOUSE_ID = "0b49b7d78734ad5c"

#: Pre-computed slide availability summary table (written nightly by the Asset Bundle job)
SUMMARY_TABLE = "cdsi_prod.pathology_data_mining.sample_wsi_summary"
