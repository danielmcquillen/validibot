# ruff: noqa: INP001
"""
Generate a Google Cloud architecture diagram for Validibot.

This script creates a PNG diagram showing the complete multi-environment
GCP architecture using official Google Cloud icons.

Usage:
    uv run --with diagrams python scripts/generate_architecture_diagram.py
"""

import logging

from diagrams import Cluster
from diagrams import Diagram
from diagrams import Edge
from diagrams.gcp.compute import Run
from diagrams.gcp.database import SQL
from diagrams.gcp.devtools import Scheduler
from diagrams.gcp.operations import Logging
from diagrams.gcp.security import Iam
from diagrams.gcp.security import KeyManagementService
from diagrams.gcp.security import SecretManager
from diagrams.gcp.storage import GCS
from diagrams.gcp.storage import Storage

logger = logging.getLogger(__name__)

# Professional color palette (GCP-inspired)
BLUE_PRIMARY = "#1a73e8"  # Google Blue
BLUE_DARK = "#174ea6"
GREEN_SUCCESS = "#1e8e3e"  # Google Green
GRAY_DARK = "#5f6368"
GRAY_LIGHT = "#9aa0a6"
RED_ACCENT = "#d93025"


def create_environment(name: str, suffix: str, *, is_prod: bool = False) -> Run:
    """Create an environment cluster with consistent layout."""
    display_suffix = "" if is_prod else f"-{suffix}"

    with Cluster(name, graph_attr={"bgcolor": "#f8f9fa", "pencolor": GRAY_LIGHT}):
        # Row 1: Support services (top)
        with Cluster("", graph_attr={"style": "invis"}):
            scheduler = Scheduler("Cloud Scheduler")
            SecretManager(f"django-env{display_suffix}")

        # Row 2: Compute services
        with Cluster(
            "Cloud Run",
            graph_attr={
                "bgcolor": "#e8f0fe",
                "pencolor": BLUE_PRIMARY,
            },
        ):
            web = Run(f"web{display_suffix}")
            worker = Run(f"worker{display_suffix}")

        # Row 3: Validators
        with Cluster(
            "Validators",
            graph_attr={
                "bgcolor": "#e8f0fe",
                "pencolor": BLUE_PRIMARY,
            },
        ):
            eplus = Run(f"energyplus{display_suffix}")
            fmu = Run(f"fmu{display_suffix}")

        # Row 4: Data layer
        db = SQL(f"validibot-db{display_suffix}\nPostgreSQL 17")
        if is_prod:
            tasks_name = "validibot-tasks"
        else:
            tasks_name = f"validibot-validation-queue-{suffix}"
        tasks = Scheduler(tasks_name)

        # Row 5: Storage (using Storage icon for bucket appearance)
        with Cluster(
            "Storage",
            graph_attr={
                "bgcolor": "#e6f4ea",
                "pencolor": GREEN_SUCCESS,
            },
        ):
            media = Storage(f"media{display_suffix}\n(public)")
            files = Storage(f"files{display_suffix}\n(private)")

        # Key data flows only - simplified connections
        # Database connections (blue)
        web >> Edge(color=BLUE_PRIMARY) >> db
        worker >> Edge(color=BLUE_PRIMARY) >> db
        eplus >> Edge(color=BLUE_PRIMARY) >> db
        fmu >> Edge(color=BLUE_PRIMARY) >> db

        # Task queue flow (gray)
        web >> Edge(color=GRAY_DARK) >> tasks
        tasks >> Edge(color=GRAY_DARK) >> worker

        # Scheduler trigger (red dashed)
        scheduler >> Edge(color=RED_ACCENT, style="dashed") >> worker

        # Storage connections (green)
        worker >> Edge(color=GREEN_SUCCESS) >> files
        web >> Edge(color=GREEN_SUCCESS) >> media
        eplus >> Edge(color=GREEN_SUCCESS) >> files
        fmu >> Edge(color=GREEN_SUCCESS) >> files

        return web


def main():
    """Generate the architecture diagram."""
    graph_attr = {
        "fontsize": "14",
        "fontname": "Helvetica",
        "bgcolor": "white",
        "pad": "0.5",
        "splines": "ortho",  # Straight orthogonal lines
        "nodesep": "0.6",
        "ranksep": "0.8",
        "concentrate": "true",  # Merge edges going to same node
    }

    node_attr = {
        "fontname": "Helvetica",
        "fontsize": "11",
    }

    with Diagram(
        "Validibot - Google Cloud Architecture",
        filename="docs/dev_docs/images/validibot_gcp_architecture",
        outformat=["png", "svg"],  # Generate both PNG and SVG
        show=False,
        direction="TB",
        graph_attr=graph_attr,
        node_attr=node_attr,
    ):
        # Shared Services cluster
        with Cluster(
            "Shared Services",
            graph_attr={
                "bgcolor": "#fef7e0",
                "pencolor": "#f9ab00",
            },
        ):
            GCS("Artifact Registry")
            Logging("Cloud Logging")
            KeyManagementService("Cloud KMS")
            Iam("IAM")

        # Create the three environments in consistent order
        create_environment("Production", "prod", is_prod=True)
        create_environment("Staging", "staging")
        create_environment("Development", "dev")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
    logger.info("✓ Architecture diagrams generated:")
    logger.info("  - docs/dev_docs/images/validibot_gcp_architecture.png")
    logger.info("  - docs/dev_docs/images/validibot_gcp_architecture.svg")
    logger.info("    (editable in Sketch)")
