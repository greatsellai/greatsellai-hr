from __future__ import annotations

from fastapi.testclient import TestClient


_MALICIOUS_HTML_RESUME = b"""<!doctype html>
<html><body>
<h1>Synthetic candidate profile</h1>
<p>Python SQL FastAPI distributed systems experience.</p>
<script>window.__candidate_controlled_script__ = true</script>
</body></html>
"""


def _upload_html_resume(client: TestClient) -> str:
    uploaded = client.post(
        "/v1/resumes/upload",
        files={"file": ("untrusted-resume.html", _MALICIOUS_HTML_RESUME, "text/html")},
    )
    assert uploaded.status_code == 200, uploaded.text
    return uploaded.json()["resume_id"]


def _assert_forced_download(response) -> None:
    assert response.status_code == 200, response.text
    assert response.content == _MALICIOUS_HTML_RESUME
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "untrusted-resume.html" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "text/html" not in response.headers["content-type"]


def test_html_original_never_renders_inline_for_a_view_grant(client: TestClient) -> None:
    resume_id = _upload_html_resume(client)

    granted = client.post(
        f"/v1/resumes/{resume_id}/file-access",
        json={"purpose": "view"},
    )
    assert granted.status_code == 200, granted.text

    _assert_forced_download(client.get(granted.json()["access_url"]))


def test_html_original_never_renders_inline_for_a_download_grant_or_compatibility_route(
    client: TestClient,
) -> None:
    resume_id = _upload_html_resume(client)

    granted = client.post(
        f"/v1/resumes/{resume_id}/file-access",
        json={"purpose": "download"},
    )
    assert granted.status_code == 200, granted.text
    _assert_forced_download(client.get(granted.json()["access_url"]))

    _assert_forced_download(client.get(f"/v1/resumes/{resume_id}/original-file"))
