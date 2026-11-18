"""Web front end tests. Run: python -m unittest discover -s tests -v

Skipped entirely when the `web` extra is not installed, so the core suite
still runs on a bare `pip install -e .`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    from sqlalchemy import select, update
except ImportError:                              # pragma: no cover - env-dependent
    TestClient = None

if TestClient is not None:
    from locqa import Config, load, run_checks
    from locqa_web import jobs, security
    from locqa_web.app import COOKIE, create_app
    from locqa_web.db import utcnow
    from locqa_web.models import DONE, FAILED, QUEUED, RUNNING, AuthSession, Run
    from locqa_web.settings import Settings, normalize_db_url

SAMPLES = os.path.join(os.path.dirname(__file__), "..", "samples")
PASSWORD = "correct horse"


def _sample(name: str) -> bytes:
    with open(os.path.join(SAMPLES, name), "rb") as fh:
        return fh.read()


@unittest.skipIf(TestClient is None, "web extra not installed: pip install -e .[web]")
class WebCase(unittest.TestCase):
    """Each test gets its own SQLite file and app. The worker is driven by
    hand (jobs.run_once) unless a test is specifically about the thread."""

    settings_overrides: dict = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(**{
            "database_url": f"sqlite:///{self._tmp.name}/test.db",
            "inline_worker": False,
            **self.settings_overrides,
        })
        self.app = create_app(self.settings)
        self.Session = self.app.state.Session
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.app.state.engine.dispose()
        self._tmp.cleanup()

    # -- helpers -------------------------------------------------------------

    def register(self, email="kestrel@example.com", client=None):
        client = client or self.client
        r = client.post("/register", data={"email": email, "password": PASSWORD},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        return client

    def csrf(self, client=None):
        page = (client or self.client).get("/runs/new")
        return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    def upload(self, name, data, config="default", translation=None, client=None,
               csrf=None):
        client = client or self.client
        files = {"file": (name, data, "application/octet-stream")}
        if translation:
            files["translation"] = (translation[0], translation[1], "application/octet-stream")
        return client.post(
            "/runs", data={"csrf": csrf or self.csrf(client), "config": config},
            files=files, follow_redirects=False,
        )

    def run_id(self, response) -> int:
        self.assertEqual(response.status_code, 303, response.text[:500])
        return int(response.headers["location"].rsplit("/", 1)[1])

    def drain(self):
        while jobs.run_once(self.Session, self.settings) is not None:
            pass

    def row(self, run_id) -> Run:
        with self.Session() as s:
            return s.get(Run, run_id)

    def other_client(self, email="aldis@example.com"):
        client = TestClient(self.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return self.register(email, client)


class TestAuth(WebCase):
    def test_register_sets_an_httponly_session_cookie(self):
        r = self.client.post("/register", data={"email": "K@Example.com ", "password": PASSWORD},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        cookie = r.headers["set-cookie"]
        self.assertIn(f"{COOKIE}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertEqual(self.client.get("/runs").status_code, 200)

    def test_session_token_is_stored_hashed(self):
        self.register()
        token = self.client.cookies.get(COOKIE)
        with self.Session() as s:
            stored = s.scalars(select(AuthSession.token_hash)).all()
        self.assertEqual(stored, [security.token_hash(token)])
        self.assertNotIn(token, stored)

    def test_duplicate_email_and_short_password_rejected(self):
        self.register()
        fresh = TestClient(self.app)
        r = fresh.post("/register", data={"email": "kestrel@example.com", "password": PASSWORD})
        self.assertEqual(r.status_code, 400)
        self.assertIn("already exists", r.text)
        r = fresh.post("/register", data={"email": "new@example.com", "password": "short"})
        self.assertEqual(r.status_code, 400)

    def test_login_wrong_password_then_right(self):
        self.register()
        fresh = TestClient(self.app)
        r = fresh.post("/login", data={"email": "kestrel@example.com", "password": "nope nope"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("do not match", r.text)
        r = fresh.post("/login", data={"email": "kestrel@example.com", "password": PASSWORD,
                                       "next": "/configs"}, follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/configs"))

    def test_login_next_cannot_redirect_off_site(self):
        self.register()
        fresh = TestClient(self.app)
        for evil in ("//evil.example", "https://evil.example", "/\\evil.example"):
            r = fresh.post("/login", data={"email": "kestrel@example.com",
                                           "password": PASSWORD, "next": evil},
                           follow_redirects=False)
            self.assertEqual(r.headers["location"], "/runs", evil)

    def test_logout_invalidates_the_session(self):
        self.register()
        token = self.client.cookies.get(COOKIE)
        r = self.client.post("/logout", data={"csrf": self.csrf()}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        stale = TestClient(self.app, cookies={COOKIE: token})
        self.assertEqual(stale.get("/runs", follow_redirects=False).status_code, 303)

    def test_pages_redirect_and_api_401_when_signed_out(self):
        r = self.client.get("/runs/new", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login?next=/runs/new")
        self.assertEqual(self.client.get("/api/runs").status_code, 401)

    def test_password_hash_round_trip(self):
        stored = security.hash_password(PASSWORD)
        self.assertNotIn(PASSWORD, stored)
        self.assertTrue(security.verify_password(PASSWORD, stored))
        self.assertFalse(security.verify_password(PASSWORD + "x", stored))
        self.assertFalse(security.verify_password(PASSWORD, "garbage"))


class TestRuns(WebCase):
    def test_upload_queues_then_worker_completes(self):
        self.register()
        run_id = self.run_id(self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv"),
                                         config="sample"))
        self.assertEqual(self.row(run_id).status, QUEUED)
        self.assertEqual(self.client.get(f"/api/runs/{run_id}").json()["status"], QUEUED)

        self.drain()
        run = self.row(run_id)
        self.assertEqual(run.status, DONE, run.error)

        # The same counts the CLI path produces for the same inputs.
        expected = run_checks(
            load(os.path.join(SAMPLES, "aetherbound_ui.csv")),
            Config.load(os.path.join(SAMPLES, "aetherbound.locqa.yml")),
        )
        summary = self.client.get(f"/api/runs/{run_id}").json()["summary"]
        self.assertEqual(summary["total"], len(expected))

        page = self.client.get(f"/runs/{run_id}")
        self.assertIn(f'src="/runs/{run_id}/report"', page.text)

    def test_report_is_served_sandboxed(self):
        self.register()
        run_id = self.run_id(self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv")))
        self.drain()
        r = self.client.get(f"/runs/{run_id}/report")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<!doctype html>", r.text)
        self.assertTrue(r.headers["content-security-policy"].startswith("sandbox allow-scripts"))
        r = self.client.get(f"/runs/{run_id}/report?download=1")
        self.assertIn("attachment", r.headers["content-disposition"])

    def test_findings_json_download(self):
        self.register()
        run_id = self.run_id(self.upload("aetherbound_ch1.xlf", _sample("aetherbound_ch1.xlf")))
        self.drain()
        body = self.client.get(f"/runs/{run_id}/findings.json").json()
        checks = {f["check"] for f in body["findings"]}
        self.assertIn("placeholder.parity", checks)
        self.assertEqual(body["summary"]["total"], len(body["findings"]))

    def test_renpy_with_translation_file(self):
        self.register()
        run_id = self.run_id(self.upload(
            "aetherbound.rpy", _sample("aetherbound.rpy"), config="sample",
            translation=("aetherbound_ja.rpy", _sample("aetherbound_ja.rpy")),
        ))
        self.drain()
        summary = json.loads(self.row(run_id).summary_json)
        self.assertTrue(summary["has_graph"])
        self.assertIn("graph.unreachable", summary["by_check"])

    def test_sample_button(self):
        self.register()
        r = self.client.post("/runs/sample", data={"csrf": self.csrf(), "which": "sheet"},
                             follow_redirects=False)
        run_id = self.run_id(r)
        self.drain()
        self.assertEqual(self.row(run_id).status, DONE)

    def test_runs_are_private_to_their_owner(self):
        self.register()
        run_id = self.run_id(self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv")))
        self.drain()
        other = self.other_client()
        for path in (f"/runs/{run_id}", f"/runs/{run_id}/report",
                     f"/runs/{run_id}/findings.json", f"/api/runs/{run_id}"):
            self.assertEqual(other.get(path).status_code, 404, path)
        self.assertEqual(other.get("/api/runs").json(), [])
        r = other.post(f"/runs/{run_id}/delete", data={"csrf": self.csrf(other)})
        self.assertEqual(r.status_code, 404)
        self.assertIsNotNone(self.row(run_id))

    def test_csrf_token_required(self):
        self.register()
        r = self.client.post("/runs", data={"config": "default"},
                             files={"file": ("a.csv", b"key,en,ja\n", "text/csv")})
        self.assertEqual(r.status_code, 403)
        r = self.upload("a.csv", b"source,target\nHi,\xe3\x81\x82\n", csrf="forged")
        self.assertEqual(r.status_code, 403)

    def test_unsupported_extension_rejected_on_the_form(self):
        self.register()
        r = self.upload("strings.xlsx", b"PK\x03\x04")
        self.assertEqual(r.status_code, 400)
        self.assertIn("cannot read .xlsx", r.text)
        with self.Session() as s:
            self.assertEqual(s.scalars(select(Run)).all(), [])

    def test_translation_only_for_renpy(self):
        self.register()
        r = self.upload("ui.csv", _sample("aetherbound_ui.csv"),
                        translation=("ja.rpy", b"translate japanese x:\n"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("only applies to Ren", r.text)

    def test_unparseable_file_fails_without_retry_and_can_be_retried(self):
        self.register()
        run_id = self.run_id(self.upload("broken.xlf", b"<xliff version='2.0'><unit"))
        self.drain()
        run = self.row(run_id)
        self.assertEqual((run.status, run.attempts), (FAILED, 1))
        self.assertIn("not well-formed", run.error)
        self.assertIn("not well-formed", self.client.get(f"/runs/{run_id}").text)

        r = self.client.post(f"/runs/{run_id}/retry", data={"csrf": self.csrf()},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual((self.row(run_id).status, self.row(run_id).attempts), (QUEUED, 0))

    def test_xliff_doctype_is_refused(self):
        self.register()
        evil = (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
                b'<xliff version="2.0"/>')
        run_id = self.run_id(self.upload("evil.xlf", evil))
        self.drain()
        self.assertIn("DOCTYPE", self.row(run_id).error)

    def test_non_utf8_sheet_gets_a_plain_message(self):
        self.register()
        run_id = self.run_id(self.upload("sjis.csv", "key,en,ja\nA,Hi,こんにちは\n".encode("cp932")))
        self.drain()
        self.assertIn("not UTF-8", self.row(run_id).error)

    def test_delete_run(self):
        self.register()
        run_id = self.run_id(self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv")))
        r = self.client.post(f"/runs/{run_id}/delete", data={"csrf": self.csrf()},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(self.row(run_id))

    def test_security_headers_on_pages(self):
        r = self.client.get("/")
        self.assertIn("frame-ancestors 'none'", r.headers["content-security-policy"])
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")

    def test_healthz(self):
        self.assertTrue(self.client.get("/healthz").json()["ok"])

    def test_html_404_page(self):
        self.register()
        r = self.client.get("/runs/999")
        self.assertEqual(r.status_code, 404)
        self.assertIn("No such run", r.text)
        self.assertIn("text/html", r.headers["content-type"])


class TestUploadLimit(WebCase):
    settings_overrides = {"max_upload_bytes": 1024}

    def test_file_over_the_limit_rejected_on_the_form(self):
        # Over the per-file limit, under the request-size guard.
        self.register()
        r = self.upload("big.csv", b"source,target\n" + b"a,b\n" * 400)
        self.assertEqual(r.status_code, 400)
        self.assertIn("larger than", r.text)

    def test_huge_request_refused_before_it_is_read(self):
        self.register()
        r = self.upload("huge.csv", b"a,b\n" * 5000)
        self.assertEqual(r.status_code, 413)
        self.assertIn("too large", r.text)
        with self.Session() as s:
            self.assertEqual(s.scalars(select(Run)).all(), [])


class TestConfigs(WebCase):
    def save(self, name, yaml_text):
        return self.client.post("/configs", data={
            "csrf": self.csrf(), "name": name, "yaml_text": yaml_text,
        })

    def test_invalid_configs_are_rejected(self):
        self.register()
        for yaml_text, message in [
            ("charset: [", "not valid YAML"),
            ("glosary: {}", "unknown config key"),
            ("severity_overrides:\n  placeholder.parit: info", "did you mean"),
            ("min_reach: 5", "min_reach"),
        ]:
            r = self.save("Broken", yaml_text)
            self.assertEqual(r.status_code, 400, yaml_text)
            self.assertIn(message, r.text, yaml_text)

    def test_saved_config_is_offered_and_snapshotted(self):
        self.register()
        self.assertEqual(self.save("Strict", "charset: shift_jis\n").status_code, 200)
        page = self.client.get("/runs/new").text
        choice = re.search(r'<option value="(profile:\d+)">Strict</option>', page).group(1)

        run_id = self.run_id(self.upload("ui.csv", _sample("aetherbound_ui.csv"), config=choice))
        self.save("Strict", "charset: cp932\n")             # edited after the run was made
        run = self.row(run_id)
        self.assertEqual((run.config_name, run.config_yaml), ("Strict", "charset: shift_jis\n"))

    def test_someone_elses_config_cannot_be_used(self):
        self.register()
        self.save("Mine", "charset: shift_jis\n")
        choice = re.search(r'value="(profile:\d+)"', self.client.get("/runs/new").text).group(1)
        other = self.other_client()
        r = self.upload("ui.csv", _sample("aetherbound_ui.csv"), config=choice, client=other)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Unknown config", r.text)


class TestQueue(WebCase):
    def make_run(self) -> int:
        self.register()
        return self.run_id(self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv")))

    def test_a_run_is_claimed_once(self):
        run_id = self.make_run()
        self.assertEqual(jobs.claim(self.Session, self.settings), (run_id, 1))
        self.assertIsNone(jobs.claim(self.Session, self.settings))

    def test_lapsed_lease_is_reclaimed_and_the_stale_result_discarded(self):
        run_id = self.make_run()
        self.assertEqual(jobs.claim(self.Session, self.settings), (run_id, 1))
        # Worker 1 goes silent; its lease runs out.
        with self.Session.begin() as s:
            s.execute(update(Run).where(Run.id == run_id)
                      .values(locked_until=utcnow() - timedelta(seconds=1)))
        self.assertEqual(jobs.claim(self.Session, self.settings), (run_id, 2))

        # Worker 1 wakes up and tries to write its result: fenced off.
        self.assertEqual(jobs.execute(self.Session, self.settings, run_id, 1), "superseded")
        self.assertEqual(self.row(run_id).status, RUNNING)
        self.assertEqual(jobs.execute(self.Session, self.settings, run_id, 2), DONE)

    def test_run_failed_after_max_attempts_of_lost_workers(self):
        run_id = self.make_run()
        with self.Session.begin() as s:
            s.execute(update(Run).where(Run.id == run_id).values(
                status=RUNNING, attempts=self.settings.max_attempts,
                locked_until=utcnow() - timedelta(seconds=1),
            ))
        self.assertIsNone(jobs.claim(self.Session, self.settings))
        run = self.row(run_id)
        self.assertEqual(run.status, FAILED)
        self.assertIn("stopped responding", run.error)

    def test_oldest_run_first(self):
        first = self.make_run()
        second = self.run_id(self.upload("aetherbound_ch1.xlf", _sample("aetherbound_ch1.xlf")))
        self.assertEqual(jobs.claim(self.Session, self.settings)[0], first)
        self.assertEqual(jobs.claim(self.Session, self.settings)[0], second)


class TestTimeout(WebCase):
    # Shorter than a Python interpreter takes to start, so every run times out.
    settings_overrides = {"job_timeout_seconds": 0.01}

    def test_slow_run_is_killed_and_failed(self):
        self.register()
        run_id = self.run_id(self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv")))
        started = time.monotonic()
        self.drain()
        self.assertLess(time.monotonic() - started, 10)
        run = self.row(run_id)
        self.assertEqual((run.status, run.attempts), (FAILED, 1))
        self.assertIn("longer than", run.error)


class TestInlineWorker(WebCase):
    settings_overrides = {"inline_worker": True, "poll_interval_seconds": 0.05}

    def test_background_thread_finishes_the_run(self):
        self.register()
        run_id = self.run_id(self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv")))
        deadline = time.monotonic() + 20
        status = None
        while time.monotonic() < deadline:
            status = self.client.get(f"/api/runs/{run_id}").json()["status"]
            if status in (DONE, FAILED):
                break
            time.sleep(0.1)
        self.assertEqual(status, DONE)


@unittest.skipIf(TestClient is None, "web extra not installed")
class TestSettings(unittest.TestCase):
    def test_postgres_urls_use_psycopg3(self):
        self.assertEqual(normalize_db_url("postgres://u:p@h/db"), "postgresql+psycopg://u:p@h/db")
        self.assertEqual(normalize_db_url("postgresql://u@h/db"), "postgresql+psycopg://u@h/db")
        self.assertEqual(normalize_db_url("sqlite:///x.db"), "sqlite:///x.db")

    def test_settings_are_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Settings().inline_worker = False


if __name__ == "__main__":
    unittest.main(verbosity=2)
