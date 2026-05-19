"""Subprocess entry point for killable SPARQL ASK evaluation.

The SHACL engine invokes this module with ``python -m`` and passes a JSON
payload on stdin. Keeping author-supplied SPARQL in a short-lived subprocess
means a timeout can terminate the query instead of leaving ``rdflib`` running
inside the Django or Celery worker.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from typing import Any

from rdflib import Graph

from validibot.validations.validators.shacl.sparql_security import SparqlScrubError
from validibot.validations.validators.shacl.sparql_security import scrub_sparql_ask

logger = logging.getLogger(__name__)


def main() -> int:
    """Read one ASK payload from stdin and write one JSON result to stdout."""

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as exc:
        _write_result({"status": "error", "body": f"Invalid SPARQL payload: {exc}"})
        return 1

    with contextlib.redirect_stdout(sys.stderr):
        result = _run_ask(payload)
    _write_result(result)
    return 0


def _run_ask(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute the ASK query against the serialized graph."""

    try:
        query = str(payload["query"])
        scrub_sparql_ask(query)

        graph = Graph()
        graph.parse(data=payload["graph_ntriples"], format="nt")
        qres = graph.query(query)
        answer = getattr(qres, "askAnswer", None)
        if answer is None:
            answer = bool(qres)
        return {"status": "ok", "answer": bool(answer)}
    except SparqlScrubError as exc:
        return {"status": "error", "body": f"SPARQL ASK rejected: {exc}"}
    except Exception as exc:
        logger.warning("SPARQL ASK raised", extra={"exc_type": type(exc).__name__})
        return {
            "status": "error",
            "body": f"SPARQL ASK execution failed: {type(exc).__name__}: {exc}",
        }


def _write_result(result: dict[str, Any]) -> None:
    """Emit the worker response as the only stdout payload."""

    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
