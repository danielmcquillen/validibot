"""
Seed data for the THERM system validator.

The THERM validator is a simple/inline validator that parses THMX and THMZ
files and extracts structured signals for downstream assertion evaluation.
It does not run simulations - it reads values directly from the XML.

Catalog entries define the output signals that workflow authors can
reference when building assertion rulesets (e.g. NFRC 100 compliance).
"""

from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import ValidationType

THERM_SEED = {
    "validator": {
        "slug": "therm-validator",
        "name": "THERM Validator",
        "description": (
            "Validate LBNL THERM thermal analysis files (THMX/THMZ). "
            "Checks geometry closure, material property ranges, boundary "
            "condition completeness, and reference integrity. Extracts "
            "signals for downstream compliance assertions."
        ),
        "validation_type": ValidationType.THERM,
        "version": "1.0",
        "order": 30,
        "has_processor": False,
        "is_system": True,
    },
    "catalog_entries": [
        # -- Counts --
        {
            "slug": "polygon_count",
            "label": "Polygon Count",
            "entry_type": CatalogEntryType.SIGNAL,
            "run_stage": CatalogRunStage.OUTPUT,
            "data_type": CatalogValueType.NUMBER,
            "order": 10,
        },
        {
            "slug": "material_count",
            "label": "Material Count",
            "entry_type": CatalogEntryType.SIGNAL,
            "run_stage": CatalogRunStage.OUTPUT,
            "data_type": CatalogValueType.NUMBER,
            "order": 20,
        },
        {
            "slug": "bc_count",
            "label": "BC Count",
            "entry_type": CatalogEntryType.SIGNAL,
            "run_stage": CatalogRunStage.OUTPUT,
            "data_type": CatalogValueType.NUMBER,
            "order": 30,
        },
        # -- Geometry --
        {
            "slug": "geometry_width_mm",
            "label": "Geometry Width",
            "entry_type": CatalogEntryType.SIGNAL,
            "run_stage": CatalogRunStage.OUTPUT,
            "data_type": CatalogValueType.NUMBER,
            "order": 40,
        },
        {
            "slug": "geometry_height_mm",
            "label": "Geometry Height",
            "entry_type": CatalogEntryType.SIGNAL,
            "run_stage": CatalogRunStage.OUTPUT,
            "data_type": CatalogValueType.NUMBER,
            "order": 50,
        },
        {
            "slug": "all_polygons_closed",
            "label": "All Polygons Closed",
            "entry_type": CatalogEntryType.SIGNAL,
            "run_stage": CatalogRunStage.OUTPUT,
            "data_type": CatalogValueType.BOOLEAN,
            "order": 60,
        },
        # -- Boundary conditions --
        {
            "slug": "interior_bc_temp",
            "label": "Interior BC Temperature",
            "entry_type": CatalogEntryType.SIGNAL,
            "run_stage": CatalogRunStage.OUTPUT,
            "data_type": CatalogValueType.NUMBER,
            "order": 70,
        },
        {
            "slug": "exterior_bc_temp",
            "label": "Exterior BC Temperature",
            "entry_type": CatalogEntryType.SIGNAL,
            "run_stage": CatalogRunStage.OUTPUT,
            "data_type": CatalogValueType.NUMBER,
            "order": 80,
        },
        {
            "slug": "interior_film_coeff",
            "label": "Interior Film Coefficient",
            "entry_type": CatalogEntryType.SIGNAL,
            "run_stage": CatalogRunStage.OUTPUT,
            "data_type": CatalogValueType.NUMBER,
            "order": 90,
        },
        {
            "slug": "exterior_film_coeff",
            "label": "Exterior Film Coefficient",
            "entry_type": CatalogEntryType.SIGNAL,
            "run_stage": CatalogRunStage.OUTPUT,
            "data_type": CatalogValueType.NUMBER,
            "order": 100,
        },
        # -- U-factor tags --
        {
            "slug": "ufactor_tags_found",
            "label": "U-Factor Tags",
            "entry_type": CatalogEntryType.SIGNAL,
            "run_stage": CatalogRunStage.OUTPUT,
            "data_type": CatalogValueType.OBJECT,
            "order": 110,
        },
        # -- Mesh --
        {
            "slug": "mesh_level",
            "label": "Mesh Level",
            "entry_type": CatalogEntryType.SIGNAL,
            "run_stage": CatalogRunStage.OUTPUT,
            "data_type": CatalogValueType.NUMBER,
            "order": 120,
        },
        # -- Flags --
        {
            "slug": "has_cma_data",
            "label": "Has CMA Data",
            "entry_type": CatalogEntryType.SIGNAL,
            "run_stage": CatalogRunStage.OUTPUT,
            "data_type": CatalogValueType.BOOLEAN,
            "order": 130,
        },
        {
            "slug": "has_glazing_system",
            "label": "Has Glazing System",
            "entry_type": CatalogEntryType.SIGNAL,
            "run_stage": CatalogRunStage.OUTPUT,
            "data_type": CatalogValueType.BOOLEAN,
            "order": 140,
        },
        # -- Version --
        {
            "slug": "therm_version",
            "label": "THERM Version",
            "entry_type": CatalogEntryType.SIGNAL,
            "run_stage": CatalogRunStage.OUTPUT,
            "data_type": CatalogValueType.STRING,
            "order": 150,
        },
    ],
}
