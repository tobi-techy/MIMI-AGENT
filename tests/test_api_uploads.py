"""Console attachments in demo mode (``mimi.api.uploads``): photos, voice
notes and PDFs the owner attaches in the console.

Offline: a scripted model and a SQLite file per test; no naira spent.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.api.media import MAX_BYTES
from mimi.api.uploads import MAX_FILES, ConsoleUploads, UploadRefused, base_mime, sniff
from mimi.harness.gateway import ScriptedModel
from mimi.store import ensure_schema, make_engine
from tests.test_api import script
from tests.test_api_demo import bearer, cfg, start

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
OGG = b"OggS" + b"\x00" * 200
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 200
M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 200
PDF = b"%PDF-1.7\n" + b"\x00" * 200


@pytest.fixture()
def env(store_url: str, tmp_path: Path) -> Iterator[dict[str, Any]]:
    engine = make_engine(store_url)
    ensure_schema(engine)
    agent = Agent(engine, models=[ScriptedModel(script)], worker_id="test-worker")
    app = create_app(Settings(mode="demo", demo=cfg(), wait_s=5, media_dir=str(tmp_path)),
                     agent, workers=0)
    with TestClient(app) as c:
        yield {"c": c, "engine": engine, "dir": tmp_path, "app": app}
    engine.dispose()


def upload(c: TestClient, sess: dict[str, Any], data: bytes, mime: str) -> Any:
    return c.post("/v1/media", content=data, headers={**bearer(sess), "Content-Type": mime})


def test_a_photo_is_kept_and_a_turn_can_point_at_it(env: dict[str, Any]) -> None:
    c = env["c"]
    sess = start(c)
    r = upload(c, sess, JPEG, "image/jpeg")
    assert r.status_code == 201, r.text
    m = r.json()
    sha = hashlib.sha256(JPEG).hexdigest()
    assert m == {"ref": f"local:console/{sess['tenant_id']}/{sha}.jpg", "sha256": sha,
                 "mime": "image/jpeg", "kind": "image", "size": len(JPEG)}
    assert (env["dir"] / "console" / sess["tenant_id"] / f"{sha}.jpg").read_bytes() == JPEG
    t = c.post("/v1/turns", headers=bearer(sess), json={
        "message_id": "console-1", "source": "owner_message", "text": "",
        "media": {k: m[k] for k in ("kind", "ref", "sha256", "mime")}})
    assert t.status_code == 200, t.text
    th = c.get(f"/v1/threads/{t.json()['thread_id']}", headers=bearer(sess)).json()
    [mine] = [x for x in th["messages"] if x["message_id"] == "in:console-1"]
    assert mine["kind"] == "image" and mine["media"] is True and mine["voice"] is False
    got = c.get("/v1/messages/in:console-1/media", headers=bearer(sess))
    assert got.status_code == 200 and got.content == JPEG
    assert got.headers["content-type"] == "image/jpeg"
    assert got.headers["cache-control"] == "private, no-store"
    assert got.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(("data", "mime", "kind"), [
    (PNG, "image/png", "image"), (OGG, "audio/ogg; codecs=opus", "voice"),
    (WEBM, "audio/webm;codecs=opus", "voice"), (M4A, "audio/mp4", "voice"),
    (PDF, "application/pdf", "document")])
def test_voice_notes_and_documents_are_kept_too(env: dict[str, Any], data: bytes, mime: str,
                                               kind: str) -> None:
    sess = start(env["c"])
    r = upload(env["c"], sess, data, mime)
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == kind and r.json()["mime"] == base_mime(mime)


def test_wrong_type_renamed_file_empty_and_too_big_are_refused(env: dict[str, Any]) -> None:
    c = env["c"]
    sess = start(c)
    assert upload(c, sess, b"GIF89a" + b"\x00" * 50, "image/gif").status_code == 415
    renamed = upload(c, sess, PDF, "image/jpeg")
    assert renamed.status_code == 415 and "not the type" in renamed.json()["detail"]
    assert upload(c, sess, b"", "image/jpeg").status_code == 422
    big = upload(c, sess, JPEG + b"\x00" * MAX_BYTES, "image/jpeg")
    assert big.status_code == 413 and "10 MB" in big.json()["detail"]
    assert not (env["dir"] / "console" / sess["tenant_id"]).exists() or not any(
        (env["dir"] / "console" / sess["tenant_id"]).iterdir())


def test_a_turn_cannot_point_at_another_copys_upload_or_any_other_file(
        env: dict[str, Any]) -> None:
    c = env["c"]
    a, b = start(c, "198.51.100.1"), start(c, "198.51.100.1")
    m = upload(c, a, JPEG, "image/jpeg").json()
    for ref, sha in ((m["ref"], m["sha256"]),  # a's upload, sent with b's token
                     (f"local:console/{b['tenant_id']}/../{a['tenant_id']}/{m['sha256']}.jpg",
                      m["sha256"]),
                     ("local:receipts/screenshot-01.png", m["sha256"])):
        r = c.post("/v1/turns", headers=bearer(b), json={
            "message_id": f"console-x-{len(ref)}", "source": "owner_message",
            "media": {"kind": "image", "ref": ref, "sha256": sha, "mime": "image/jpeg"}})
        assert r.status_code == 422, (ref, r.text)
    # a's own upload with a wrong hash is refused as well
    r = c.post("/v1/turns", headers=bearer(a), json={
        "message_id": "console-y", "source": "owner_message",
        "media": {"kind": "image", "ref": m["ref"], "sha256": "0" * 64, "mime": "image/jpeg"}})
    assert r.status_code == 422
    # and b cannot read a's attachment back
    c.post("/v1/turns", headers=bearer(a), json={
        "message_id": "console-z", "source": "owner_message",
        "media": {k: m[k] for k in ("kind", "ref", "sha256", "mime")}})
    assert c.get("/v1/messages/in:console-z/media", headers=bearer(a)).status_code == 200
    assert c.get("/v1/messages/in:console-z/media", headers=bearer(b)).status_code == 404
    assert c.get("/v1/messages/console-z/media", headers=bearer(a)).status_code == 404


def test_uploads_need_a_session_and_stop_at_the_per_copy_limit(env: dict[str, Any],
                                                              tmp_path: Path) -> None:
    c = env["c"]
    assert c.post("/v1/media", content=JPEG, headers={"Content-Type": "image/jpeg"}
                  ).status_code == 401
    store = ConsoleUploads(tmp_path / "limit")
    for i in range(MAX_FILES):
        store.put("demo-x", JPEG + bytes([i]), "image/jpeg")
    with pytest.raises(UploadRefused) as e:
        store.put("demo-x", JPEG + b"new", "image/jpeg")
    assert e.value.status == 429
    store.put("demo-x", JPEG + bytes([0]), "image/jpeg")  # the same file again is fine
    with pytest.raises(UploadRefused):
        store.put("../etc", JPEG, "image/jpeg")


def test_a_swept_copy_takes_its_uploads_with_it(env: dict[str, Any]) -> None:
    c = env["c"]
    demo = env["app"].state.demo
    sess = start(c)
    upload(c, sess, JPEG, "image/jpeg")
    folder = env["dir"] / "console" / sess["tenant_id"]
    assert folder.is_dir()
    demo.clock = lambda: 10**10  # far past the TTL and the grace
    assert demo.sweep() >= 1
    assert not folder.exists()


def test_live_mode_has_no_upload_endpoint(store_url: str) -> None:
    engine = make_engine(store_url)
    ensure_schema(engine)
    agent = Agent(engine, models=[ScriptedModel(script)], worker_id="test-worker")
    owner = "a-owner-key-000000000001"
    app = create_app(Settings(keys=parse_keys(f"coral-lane:owner:{owner}"), wait_s=5),
                     agent, workers=0)
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {owner}"}
        assert c.post("/v1/media", content=JPEG,
                      headers={**h, "Content-Type": "image/jpeg"}).status_code == 404
        assert c.get("/v1/messages/in:x/media", headers=h).status_code == 404
    engine.dispose()


def test_sniffing_reads_the_first_bytes_only() -> None:
    assert sniff(JPEG) == "image/jpeg" and sniff(PDF) == "application/pdf"
    assert sniff(b"<html>") == "" and sniff(b"") == ""
    assert base_mime("audio/x-m4a") == "audio/mp4" and base_mime("IMAGE/JPEG") == "image/jpeg"
