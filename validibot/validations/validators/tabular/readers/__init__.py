"""Pluggable readers that feed the shared Tabular Validator core.

Each reader turns a submission's bytes into the same in-memory model: a
string-valued dataframe with canonical logical column names. CSV is the
only reader in V1; TSV/Excel/Parquet are future siblings in front of the
same validation core (the point of the "Tabular", not "CSV", framing).
"""
