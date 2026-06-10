"""Canonical Tabular dataset metadata exposed through the ``i.*`` namespace."""

from django.utils.translation import gettext_lazy as _

TABULAR_DATASET_INPUTS = (
    ("num_rows", _("Number of rows")),
    ("num_columns", _("Number of columns")),
    ("column_names", _("Column names")),
    ("delimiter", _("Delimiter")),
    ("encoding", _("Encoding")),
    ("has_header", _("Header row present")),
    ("size_bytes", _("File size (bytes)")),
    ("filename", _("Filename")),
)
