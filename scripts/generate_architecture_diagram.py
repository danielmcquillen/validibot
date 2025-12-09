#!/usr/bin/env python3
"""
Generate a Google Cloud architecture diagram for Validibot.

This script creates a PNG diagram showing the complete multi-environment
GCP architecture using official Google Cloud icons.

Usage:
    uv run --with diagrams python scripts/generate_architecture_diagram.py
"""
from diagrams import Cluster, Diagram, Edge
from diagrams.gcp.compute import Run
from diagrams.gcp.database import SQL
from diagrams.gcp.devtools import Scheduler
from diagrams.gcp.operations import Logging
from diagrams.gcp.security import Iam, KeyManagementService, SecretManager
from diagrams.gcp.storage import GCS, Storage

# Professional color palette (GCP-inspired)
BLUE_PRIMARY = "#1a73e8"  # Google Blue
BLUE_DARK = "#174ea6"
GREEN_SUCCESS = "#1e8e3e"  # Google Green
GRAY_DARK = "#5f6368"
GRAY_LIGHT = "#9aa0a6"
RED_ACCENT = "#d93025"


def create_environment(name: str, suffix: str, is_prod: bool = False):
    """Create an environment cluster with consistent layout."""
    display_suffix = "" if is_prod else f"-{suffix}"

    with Cluster(name, graph_attr={"bgcolor": "#f8f9fa", "pencolor": GRAY_LIGHT}):
        # Row 1: Support services (top)
        with Cluster("", graph_attr={"style": "invis"}):
            scheduler = Scheduler(f"Cloud Scheduler")
            secret = SecretManager(f"django-env{display_suffix}")

        # Row 2: Compute services
        with Cluster("Cloud Run", graph_attr={"bgcolor": "#e8f0fe", "pencolor": BLUE_PRIMARY}):
            web = Run(f"web{display_suffix}")
            worker = Run(f"worker{display_suffix}")

        # Row 3: Validators
        with Cluster("Validators", graph_attr={"bgcolor": "#e8f0fe", "pencolor": BLUE_PRIMARY}):
            eplus = Run(f"energyplus{display_suffix}")
            fmi = Run(f"fmi{display_suffix}")

        # Row 4: Data layer
        db = SQL(f"validibot-db{display_suffix}\nPostgreSQL 17")
        tasks = Scheduler(f"validation-queue{display_suffix}")

        # Row 5: Storage (using Storage icon for bucket appearance)
        with Cluster("Storage", graph_attr={"bgcolor": "#e6f4ea", "pencolor": GREEN_SUCCESS}):
            media = Storage(f"media{display_suffix}\n(public)")
            files = Storage(f"files{display_suffix}\n(private)")

        # Key data flows only - simplified connections
        # Database connections (blue)
        web >> Edge(color=BLUE_PRIMARY) >> db
        worker >> Edge(color=BLUE_PRIMARY) >> db
        eplus >> Edge(color=BLUE_PRIMARY) >> db
        fmi >> Edge(color=BLUE_PRIMARY) >> db

        # Task queue flow (gray)
        web >> Edge(color=GRAY_DARK) >> tasks
        tasks >> Edge(color=GRAY_DARK) >> worker

        # Scheduler trigger (red dashed)
        scheduler >> Edge(color=RED_ACCENT, style="dashed") >> worker

        # Storage connections (green)
        worker >> Edge(color=GREEN_SUCCESS) >> files
        web >> Edge(color=GREEN_SUCCESS) >> media
        eplus >> Edge(color=GREEN_SUCCESS) >> files
        fmi >> Edge(color=GREEN_SUCCESS) >> files

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
        with Cluster("Shared Services", graph_attr={"bgcolor": "#fef7e0", "pencolor": "#f9ab00"}):
            registry = GCS("Artifact Registry")
            logging = Logging("Cloud Logging")
            kms = KeyManagementService("Cloud KMS")
            iam = Iam("IAM")

        # Create the three environments in consistent order
        create_environment("Production", "prod", is_prod=True)
        create_environment("Staging", "staging")
        create_environment("Development", "dev")


if __name__ == "__main__":
    main()
    print("✓ Architecture diagrams generated:")
    print("  - docs/dev_docs/images/validibot_gcp_architecture.png")
    print("  - docs/dev_docs/images/validibot_gcp_architecture.svg (editable in Sketch)")
