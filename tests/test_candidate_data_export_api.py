from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.candidate_data_export_service import run_candidate_data_export_worker_once
from test_candidate_data_export_service import _seed_exportable_candidate


def test_export_api_queues_statuses_and_serves_only_session_authorized_zip(
    client: TestClient,
) -> None:
    candidate_id, _ = _seed_exportable_candidate(client)

    created = client.post(
        "/v1/candidate-data-exports",
        json={"candidate_ids": [candidate_id], "include_originals": False},
    )
    assert created.status_code == 202, created.text
    payload = created.json()
    export_id = payload["export_id"]
    assert payload["status"] == "queued"

    listed = client.get("/v1/candidate-data-exports")
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["export_id"] == export_id

    assert run_candidate_data_export_worker_once(
        client.app.state.database,
        settings=client.app.state.settings,
        worker_id="candidate-data-export-api-test",
    )
    completed = client.get(f"/v1/candidate-data-exports/{export_id}")
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "completed"

    access = client.post(f"/v1/candidate-data-exports/{export_id}/download-access")
    assert access.status_code == 200, access.text
    download = client.get(access.json()["access_url"])
    assert download.status_code == 200, download.text
    assert download.headers["content-type"].startswith("application/zip")
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["cache-control"] == "no-store, private"
    assert download.headers["referrer-policy"] == "no-referrer"

    revoked = client.delete(f"/v1/candidate-data-exports/{export_id}")
    assert revoked.status_code == 202, revoked.text
    assert revoked.json()["status"] == "revoked"
    stale_download = client.get(access.json()["access_url"])
    assert stale_download.status_code == 404
    assert stale_download.json()["detail"] == "candidate_data_export_download_not_found"
