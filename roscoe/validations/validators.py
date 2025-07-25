import logging
from base64 import b64decode

from django_github_app.routing import GitHubRouter

gh = GitHubRouter()

logger = logging.getLogger(__name__)


@gh.event("check_suite", action="requested")  # fires on every new commit push
def validate_idf(event, gh_api, **_):
    """
    Dummy EnergyPlus IDF validator.
    • Creates an 'EnergyPlus validator' check run for the new commit.
    • Downloads every *.idf in that commit's tree.
    • Marks the run as failure if any file contains the bytes 'BAD'.
    """

    logger.info("Starting IDF validation for check suite event")
    logger.info("Event data:", extra={"event": event})

    repo_full = event.data["repository"]["full_name"]
    head_sha = event.data["check_suite"]["head_sha"]

    # 1 Start the check run so a line appears quickly in the UI
    run = gh_api.post(
        f"/repos/{repo_full}/check-runs",
        data={
            "name": "EnergyPlus validator",
            "head_sha": head_sha,
            "status": "in_progress",
        },
    )  # requires Checks **write** permission :contentReference[oaicite:0]{index=0}

    # 2️  List every blob in the commit tree and filter *.idf
    tree = gh_api.get(f"/repos/{repo_full}/git/trees/{head_sha}?recursive=1")["tree"]
    idf_paths = [item["path"] for item in tree if item["path"].endswith(".idf")]

    failed = []

    logger.info("Checking these files", extra={"idf_paths": idf_paths})
    for path in idf_paths:
        blob = gh_api.get(f"/repos/{repo_full}/contents/{path}?ref={head_sha}")[
            "content"
        ]  # base64 payload
        contents = b64decode(blob)
        # Pretend validator rule: fail if file contains 'BAD'
        if b"BAD" in contents:
            failed.append(path)

    # 3️  Finish the check run
    conclusion = "failure" if failed else "success"
    summary = (
        "❌ Files that need fixing:\n" + "\n".join(failed)
        if failed
        else "✅ All IDF files passed the dummy check"
    )

    gh_api.patch(
        f"/repos/{repo_full}/check-runs/{run['id']}",
        data={
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": "EnergyPlus IDF validation",
                "summary": summary,
            },
        },
    )
