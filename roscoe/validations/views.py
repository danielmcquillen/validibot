import subprocess
import tempfile

from django_github_app.routing import GitHubRouter

gh = GitHubRouter()


@gh.event("pull_request", action=["opened", "synchronize", "reopened"])
def validate_idf(event, gh_api, installation, **_):
    pr = event.data["pull_request"]
    repo_full = pr["base"]["repo"]["full_name"]
    number = pr["number"]

    # 1. list changed files
    files = gh_api.get(f"/repos/{repo_full}/pulls/{number}/files")
    idfs = [f for f in files if f["filename"].endswith(".idf")]
    if not idfs:
        return  # nothing to do

    # 2. download each .idf and run your validator
    failures = []
    for f in idfs:
        blob = gh_api.get(f["contents_url"])  # needs contents:read
        with tempfile.NamedTemporaryFile(suffix=".idf") as tmp:
            tmp.write(blob.decode_content())
            tmp.flush()
            # Dummy validator; replace with EnergyPlus or pyIdf
            result = subprocess.run(["idfchecker", tmp.name])
            if result.returncode != 0:
                failures.append(f["filename"])

    # 3a. POST a Check Run (preferred)
    gh_api.post(
        f"/repos/{repo_full}/check-runs",
        data={
            "name": "EnergyPlus validator",
            "head_sha": pr["head"]["sha"],
            "status": "completed",
            "conclusion": "failure" if failures else "success",
            "output": {
                "title": "EnergyPlus IDF validation",
                "summary": "❌ Failures:\n" + "\n".join(failures)
                if failures
                else "✅ All IDF files passed",
            },
        },
    )
