"""Offline lease tests: fake identities/clock, real CAS semantics, no network."""
from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_parcel as support
from test_parcel import FakeOnboarding, WORKER_DID, COORDINATOR_DID, parcel, claude_adapter


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.a = self.root / "a.json"
        self.b = self.root / "b.json"
        self.private = support.ClaimTests().private_parcel()
        self.private.update(parcel_version=2, lease_seconds=10, expected_worker_did=None)
        self.private["task"].update(v=2, type="task", task_id=self.private["task_id"], lease_seconds=10)
        for path in (self.a, self.b):
            parcel.write_private_json(path, self.private, replace=False)
        self.state = {"v": 2, "task_id": self.private["task_id"], "status": "idle",
                      "attempt": 0, "expires_ms": 0, "checkpoint": None}
        self.clock = 1000
        self.records = [{"from": COORDINATOR_DID, "text": parcel.compact_json(self.private["task"])}]
        self.worker = WORKER_DID
        self.before_cas = None
        self.fail_publish = False
        self.patches = [
            mock.patch.object(parcel, "now_ms", side_effect=lambda: self.clock),
            mock.patch.object(parcel, "read_note", side_effect=lambda *a: copy.deepcopy(self.state)),
            mock.patch.object(parcel, "request_bytes", side_effect=self.cas),
            mock.patch.object(parcel, "load_onboarding", side_effect=lambda *a: FakeOnboarding(self.worker)),
            mock.patch.object(parcel, "signed_event", side_effect=self.publish),
            mock.patch.object(parcel, "verify_record_signature", return_value=True),
            mock.patch.object(parcel, "room_export", side_effect=lambda *a: {
                "generation": "0", "sha256": "d" * 64, "records": copy.deepcopy(self.records)}),
            mock.patch.object(parcel, "service_version", return_value="test"),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def cas(self, url, payload=None):
        if self.before_cas:
            hook, self.before_cas = self.before_cas, None
            hook()
        if payload["if"] != parcel.compact_json(self.state):
            raise parcel.ClaimConflict("state changed")
        self.state = json.loads(payload["value"])
        return b"ok"

    def publish(self, private, identity, event, kind):
        if self.fail_publish:
            raise parcel.ParcelError("simulated lost publication")
        record = {"from": self.worker, "text": parcel.compact_json(event), "seq": len(self.records) + 1}
        self.records.append(record)
        receipt = {"verified_record": record}
        private["events"].append(receipt)
        return receipt

    def claim_a(self):
        return parcel.claim_parcel(self.a, Path("unused"), "worker-a")

    def event(self, path, kind, body):
        return parcel.worker_event(path, Path("unused"), kind, body)

    def test_interruption_handoff_preserves_draft_and_rejects_old_completion(self):
        old = self.claim_a()
        self.event(self.a, "progress", "First stanza")
        self.clock = old["expires_ms"]
        self.worker = COORDINATOR_DID
        replacement = parcel.claim_parcel(self.b, Path("unused"), "worker-b")
        self.assertEqual(replacement["checkpoint"]["body"], "First stanza")
        self.assertEqual(replacement["attempt"], 2)
        self.assertNotEqual(old["claim_id"], replacement["claim_id"])
        self.worker = WORKER_DID
        with self.assertRaisesRegex(parcel.ParcelError, "attempt"):
            self.event(self.a, "result", "Late original result")
        self.worker = COORDINATOR_DID
        self.event(self.b, "result", "Completed poem")
        checked = parcel.verify_parcel(self.b)
        self.assertTrue(checked["valid"], checked["errors"])
        self.assertEqual(checked["result_count"], 1)
        self.assertEqual(checked["results"][0]["event"]["body"], "Completed poem")

    def test_renewal_prevents_takeover(self):
        old = self.claim_a()
        self.clock += 5000
        new = parcel.renew_claim(self.a, Path("unused"))
        self.assertGreater(new["expires_ms"], old["expires_ms"])
        self.clock = old["expires_ms"]
        with self.assertRaises(parcel.ParcelError):
            parcel.claim_parcel(self.b, Path("unused"), "replacement")

    def test_exact_expiry_refuses_renew_and_result(self):
        self.clock = self.claim_a()["expires_ms"]
        with self.assertRaisesRegex(parcel.ParcelError, "expired"):
            parcel.renew_claim(self.a, Path("unused"))
        with self.assertRaisesRegex(parcel.ParcelError, "expired"):
            self.event(self.a, "result", "too late")

    def test_same_did_new_attempt_fences_old_copy(self):
        self.clock = self.claim_a()["expires_ms"]
        parcel.claim_parcel(self.b, Path("unused"), "same-worker-new-attempt")
        with self.assertRaisesRegex(parcel.ParcelError, "attempt"):
            self.event(self.a, "result", "old process")
        self.event(self.b, "result", "new process")

    def test_cas_race_does_not_overwrite_winner(self):
        self.claim_a()
        self.clock = self.state["expires_ms"]
        def winner():
            self.state.update(claim_id="f" * 32, attempt=2, expires_ms=self.clock + 10000)
        self.before_cas = winner
        with self.assertRaises(parcel.ClaimConflict):
            parcel.claim_parcel(self.b, Path("unused"), "loser")
        self.assertEqual(self.state["claim_id"], "f" * 32)

    def test_lost_claim_response_can_retry_with_saved_token(self):
        original = self.cas
        def lost(url, payload):
            original(url, payload)
            raise parcel.ParcelError("response lost")
        with mock.patch.object(parcel, "request_bytes", side_effect=lost):
            with self.assertRaises(parcel.ParcelError):
                self.claim_a()
        self.assertEqual(self.claim_a()["claim_id"], self.state["claim_id"])

    def test_completion_publication_retry_is_idempotent(self):
        self.claim_a()
        self.fail_publish = True
        with self.assertRaises(parcel.ParcelError):
            self.event(self.a, "result", "finished")
        self.assertEqual(self.state["status"], "working")
        self.assertFalse(parcel.verify_parcel(self.a)["valid"])
        self.fail_publish = False
        self.event(self.a, "result", "finished")
        self.clock += 50000
        self.event(self.a, "result", "finished")
        with self.assertRaisesRegex(parcel.ParcelError, "completed"):
            parcel.claim_parcel(self.b, Path("unused"), "replacement")
        checked = parcel.verify_parcel(self.a)
        self.assertTrue(checked["valid"], checked["errors"])
        self.assertEqual(checked["result_count"], 1)
        with self.assertRaisesRegex(parcel.ParcelError, "replaced"):
            self.event(self.a, "result", "different")

    def test_completion_losing_cas_race_posts_only_an_unaccepted_candidate(self):
        self.claim_a()
        self.before_cas = lambda: self.state.update(claim_id="f" * 32)
        with self.assertRaises(parcel.ClaimConflict):
            self.event(self.a, "result", "obsolete")
        self.assertEqual(len(self.records), 3)
        self.assertNotIn("result", self.state)
        self.assertFalse(parcel.verify_parcel(self.a)["valid"])

    def test_progress_losing_race_does_not_replace_checkpoint(self):
        self.claim_a()
        self.before_cas = lambda: self.state.update(claim_id="f" * 32)
        with self.assertRaises(parcel.ClaimConflict):
            self.event(self.a, "progress", "obsolete draft")
        self.assertIsNone(self.state["checkpoint"])

    def test_expired_or_missing_note_is_not_a_new_task(self):
        self.state = None
        with self.assertRaisesRegex(parcel.ParcelError, "missing"):
            self.claim_a()

    def test_unverifiable_checkpoint_stops_handoff(self):
        self.claim_a()
        self.event(self.a, "progress", "draft")
        self.clock = self.state["expires_ms"]
        self.records = self.records[:1]
        with self.assertRaisesRegex(parcel.ParcelError, "checkpoint"):
            parcel.claim_parcel(self.b, Path("unused"), "replacement")

    def test_worker_pin_still_applies(self):
        private = parcel.load_parcel(self.a)
        private["expected_worker_did"] = COORDINATOR_DID
        parcel.write_private_json(self.a, private, replace=True)
        with self.assertRaisesRegex(parcel.ParcelError, "expected worker"):
            self.claim_a()

    def test_signed_but_stale_result_is_not_accepted(self):
        old = self.claim_a()
        self.clock = old["expires_ms"]
        parcel.claim_parcel(self.b, Path("unused"), "replacement")
        self.records.append({"from": WORKER_DID, "text": parcel.compact_json({
            "v": 2, "type": "result", "task_id": self.private["task_id"],
            "worker_did": WORKER_DID, "claim_id": old["claim_id"], "attempt": 1,
            "body": "stale", "created_at": "ignored"})})
        self.event(self.b, "result", "current")
        checked = parcel.verify_parcel(self.b)
        self.assertTrue(checked["valid"], checked["errors"])
        self.assertEqual(checked["result_count"], 1)

    def test_adapter_refuses_old_attempt_before_publishing(self):
        old = self.claim_a()
        self.clock = old["expires_ms"]
        self.claim_a()
        response = self.root / "response.txt"
        response.write_text("stale result")
        with self.assertRaisesRegex(claude_adapter.parcel_module.ParcelError, "claim-id"):
            claude_adapter.submit(self.a, Path("unused"), response, old["claim_id"])

    def test_cli_requires_attempt_token_before_loading_identity(self):
        self.claim_a()
        with mock.patch("sys.argv", ["parcel.py", "result", str(self.a), "--text", "old"]), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as error, \
                mock.patch.object(parcel, "load_onboarding") as identity:
            self.assertEqual(parcel.main(), 1)
            self.assertIn("--claim-id", error.getvalue())
            identity.assert_not_called()

    def test_claim_announcement_can_retry_after_publication_failure(self):
        self.fail_publish = True
        with self.assertRaises(parcel.ParcelError):
            self.claim_a()
        token = self.state["claim_id"]
        self.fail_publish = False
        self.assertEqual(self.claim_a()["claim_id"], token)
        self.event(self.a, "result", "done")
        self.assertTrue(parcel.verify_parcel(self.a)["valid"])

    def test_completion_requires_signed_claim(self):
        self.claim_a()
        self.event(self.a, "result", "done")
        self.records = [r for r in self.records if json.loads(r["text"])["type"] != "claim"]
        checked = parcel.verify_parcel(self.a)
        self.assertFalse(checked["valid"])
        self.assertIn("parcel had no signed announcement for the current lease", checked["errors"])

    def test_forged_checkpoint_refuses_handoff(self):
        self.claim_a()
        self.event(self.a, "progress", "draft")
        self.clock = self.state["expires_ms"]
        with mock.patch.object(parcel, "verify_record_signature", return_value=False):
            with self.assertRaisesRegex(parcel.ParcelError, "checkpoint"):
                parcel.claim_parcel(self.b, Path("unused"), "replacement")

    def test_leased_creation_initializes_state_and_signed_task(self):
        path = self.root / "new.json"
        with mock.patch.object(parcel, "write_note_if_absent") as notes:
            _, private = parcel.create_parcel("Poem", "One stanza", Path("unused"),
                                             "https://example.invalid", None, path, 30)
        self.assertEqual(private["parcel_version"], 2)
        self.assertEqual(private["task"]["lease_seconds"], 30)
        self.assertEqual(notes.call_args_list[0].args[1], "claim")
        self.assertEqual(notes.call_args_list[0].args[2]["status"], "idle")
        self.assertEqual(parcel.load_parcel(path)["lease_seconds"], 30)

    def test_invalid_duration_fails_before_identity_or_network(self):
        for value in (0, -1, 86401, True, 1.5):
            with self.subTest(value=value), mock.patch.object(parcel, "load_onboarding") as identity:
                with self.assertRaisesRegex(parcel.ParcelError, "lease seconds"):
                    parcel.create_parcel("Poem", "stanza", Path("unused"), "invalid", None, None, value)
                identity.assert_not_called()

    def test_public_export_keeps_v2_and_omits_capability(self):
        self.claim_a()
        self.event(self.a, "result", "done")
        exported = parcel.export_public(self.a, self.root / "public.json")
        self.assertEqual(exported["parcel_version"], 2)
        self.assertTrue(exported["valid"])
        self.assertNotIn(self.private["room"], json.dumps(exported))

    def test_lost_completion_cas_response_can_retry_after_expiry(self):
        self.claim_a()
        original = self.cas
        def lost(url, payload):
            original(url, payload)
            raise parcel.ParcelError("response lost")
        with mock.patch.object(parcel, "request_bytes", side_effect=lost):
            with self.assertRaises(parcel.ParcelError):
                self.event(self.a, "result", "done")
        self.assertEqual(self.state["status"], "completed")
        self.clock += 50000
        self.event(self.a, "result", "done")
        self.assertEqual(parcel.verify_parcel(self.a)["result_count"], 1)

    def test_disappearance_during_result_publication_allows_replacement(self):
        self.claim_a()
        self.fail_publish = True
        with self.assertRaises(parcel.ParcelError):
            self.event(self.a, "result", "unpublished")
        self.clock = self.state["expires_ms"]
        self.fail_publish = False
        parcel.claim_parcel(self.b, Path("unused"), "replacement")
        self.event(self.b, "result", "replacement result")
        self.assertTrue(parcel.verify_parcel(self.b)["valid"])


if __name__ == "__main__":
    unittest.main()
