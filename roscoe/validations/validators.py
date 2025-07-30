import logging
from base64 import b64decode

from gidgethub import sansio
from gidgethub.abc import GitHubAPI
from gidgethub.sansio import Event

from django_github_app.routing import GitHubRouter

logger = logging.getLogger(__name__)

gh = GitHubRouter()
logger.info("GitHubRouter created in roscoe.validations.validators: %s", gh)
logger.info(" - Available routers: %s", GitHubRouter.routers)


@gh.event("check_suite", action="requested")
async def validate_idf(event: sansio.Event, gh: GitHubAPI, *args, **kwargs):
    logger.info("🎯 VALIDATE_IDF CALLED! Event: %s", event.event)

    repo_full = event.data["repository"]["full_name"]
    head_sha = event.data["check_suite"]["head_sha"]

    run = await gh.post(
        f"/repos/{repo_full}/check-runs",
        data={
            "name": "EnergyPlus validator",
            "head_sha": head_sha,
            "status": "in_progress",
        },
    )

    tree_response = await gh.get(f"/repos/{repo_full}/git/trees/{head_sha}?recursive=1")
    idf_paths = [
        item["path"] for item in tree_response["tree"] if item["path"].endswith(".idf")
    ]

    failed = []
    logger.info("Checking these files", extra={"idf_paths": idf_paths})

    for path in idf_paths:
        blob_resp = await gh.get(f"/repos/{repo_full}/contents/{path}?ref={head_sha}")
        contents = b64decode(blob_resp["content"])
        if b"BAD" in contents:
            failed.append(path)

    conclusion = "failure" if failed else "success"
    summary = (
        "❌ Files that need fixing:\n" + "\n".join(failed)
        if failed
        else "✅ All IDF files passed the dummy check"
    )

    await gh.patch(
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

    logger.info("✅ validate_idf function completed for: %s", repo_full)
