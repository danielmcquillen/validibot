from __future__ import annotations

import pytest

from simplevalidations.projects.forms import ProjectForm


def build_form(color: str):
    return ProjectForm(
        data={
            "name": "Data Quality",
            "description": "",
            "color": color,
        },
    )


def test_project_form_normalizes_hex_color():
    form = build_form("#00aa88")

    assert form.is_valid(), form.errors
    assert form.cleaned_data["color"] == "#00AA88"


def test_project_form_rejects_invalid_color():
    form = build_form("blue")

    assert not form.is_valid()
    assert "color" in form.errors


def test_project_form_prefills_random_color_for_new_instances():
    form = ProjectForm()

    initial_color = form.initial.get("color")
    assert isinstance(initial_color, str)
    assert initial_color.startswith("#")
    assert len(initial_color) == 7
    # Sanity-check: value looks like hex.
    int(initial_color[1:], 16)


def test_project_form_color_field_includes_preview_button():
    form = ProjectForm()
    html = form["color"].as_widget()
    assert "project-color-preview-btn" in html
    assert "data-color-input" in html
