import logging
from base64 import b64decode

from django.conf import settings

logger = logging.getLogger(__name__)

if getattr(settings, "GITHUB_APP_ENABLED", False):
    try:
        from django_github_app.routing import GitHubRouter

        gh = GitHubRouter()

        @gh.event("check_suite", action="requested")
        async def validate_idf(event, api):
            logger.info("🎯 VALIDATE_IDF CALLED! Event: %s", event.event)

            repo_full = event.data["repository"]["full_name"]
            head_sha = event.data["check_suite"]["head_sha"]

            run = await api.post(
                f"/repos/{repo_full}/check-runs",
                data={
                    "name": "EnergyPlus validator",
                    "head_sha": head_sha,
                    "status": "in_progress",
                },
            )

            # Retrieve the commit object first to get its tree SHA
            commit_resp = await api.getitem(
                f"/repos/{repo_full}/git/commits/{head_sha}"
            )
            tree_sha = commit_resp["tree"]["sha"]
            # Fetch the recursive tree by SHA
            tree_response = await api.getitem(
                f"/repos/{repo_full}/git/trees/{tree_sha}?recursive=1"
            )
            idf_paths = [
                item["path"]
                for item in tree_response["tree"]
                if item["path"].endswith(".idf")
            ]

            failed = []
            logger.info("Checking these files", extra={"idf_paths": idf_paths})

            for path in idf_paths:
                blob_resp = await api.getitem(
                    f"/repos/{repo_full}/contents/{path}?ref={head_sha}"
                )
                contents = b64decode(blob_resp["content"])
                if b"BAD" in contents:
                    failed.append(path)

            conclusion = "failure" if failed else "success"
            summary = (
                "❌ Files that need fixing:\n" + "\n".join(failed)
                if failed
                else "✅ All IDF files passed the dummy check"
            )

            await api.patch(
                f"/repos/{repo_full}/check-runs/{run['id']}",
                data={
                    "status": "completed",
                    "conclusion": conclusion,
                    "output": {
                        "title": "EnergyPlus validation",
                        "summary": summary,
                    },
                },
            )

            logger.info("✅ validate_idf function completed for: %s", repo_full)
            return
    except Exception as e:
        logger.debug("GitHub validators not active: %s", e)
else:
    logger.info("GitHub validators disabled (GITHUB_APP_ENABLED=False)")
