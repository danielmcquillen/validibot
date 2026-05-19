"""Subprocess entry point for running pySHACL with a killable boundary.

The main SHACL engine invokes this module with ``python -m`` and passes a
JSON payload on stdin. Keeping pySHACL in a plain subprocess matters for
Celery prefork workers: task processes are daemonic, so they cannot create
``multiprocessing.Process`` children, but they can launch and terminate a
regular OS subprocess.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from typing import Any

import pyshacl
from rdflib import Graph

logger = logging.getLogger(__name__)


def main() -> int:
    """Read the serialized graphs from stdin and write one JSON result to stdout."""
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as exc:
        _write_result({"status": "error", "body": f"Invalid SHACL payload: {exc}"})
        return 1

    with contextlib.redirect_stdout(sys.stderr):
        result = _run_pyshacl(payload)
    _write_result(result)
    return 0


def _run_pyshacl(payload: dict[str, Any]) -> dict[str, str]:
    """Execute pySHACL and return a small JSON-serializable result envelope."""
    try:
        data_graph = Graph()
        data_graph.parse(data=payload["data_graph_ntriples"], format="nt")

        shapes_graph = Graph()
        shapes_graph.parse(data=payload["shapes_graph_turtle"], format="turtle")

        ontology_graph: Graph | None = None
        if payload.get("ontology_graph_turtle"):
            ontology_graph = Graph()
            ontology_graph.parse(
                data=payload["ontology_graph_turtle"],
                format="turtle",
            )

        _conforms, results_graph, _results_text = pyshacl.validate(
            data_graph,
            shacl_graph=shapes_graph,
            ont_graph=ontology_graph,
            inference=payload["inference_mode"],
            advanced=payload["advanced_shacl"],
            max_validation_depth=payload["max_validation_depth"],
            # SECURITY: pyshacl's JavaScript-constraint engine
            # (``sh:JSConstraint``) executes attacker-controlled JS via
            # pyduktape3. We disable it unconditionally and also reject
            # JS constructs before launching this subprocess.
            js=False,
            # SECURITY: pyshacl can follow ``owl:imports`` and fetch the
            # imported ontology over the network. We hard-code False
            # and never read this from a setting.
            do_owl_imports=False,
            # Include Warning and Info findings in the report. Validibot
            # computes ``passed`` from severity counts after mapping, so
            # we want all findings in the report regardless of how
            # pyshacl interprets conformance.
            allow_warnings=True,
            allow_infos=True,
        )
        return {"status": "ok", "body": results_graph.serialize(format="turtle")}
    except Exception as exc:
        logger.exception("pyshacl.validate raised")
        return {"status": "error", "body": f"SHACL engine error: {exc}"}


def _write_result(result: dict[str, str]) -> None:
    """Emit the worker response as the only stdout payload."""
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
