from __future__ import annotations

import base64
import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


parcel = load_module("parcel", ROOT / "parcel.py")
claude_adapter = load_module("claude_adapter", ROOT / "scripts" / "claude_adapter.py")
COORDINATOR_DID = "did:key:z6MkrNkU2iHvF1YAM7JQxgzU8a8YgB6QGCCKBFzQbRmpZ1GM"
WORKER_DID = "did:key:z6Mkiy4Dv9Ukbe8mnkH8A2QqwMjbeSvYCGoc9izjq33KVt7K"


def base58_encode(value: bytes) -> str:
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = parcel.BASE58_ALPHABET[remainder] + encoded
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + encoded


def signing_identity() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    did = "did:key:z" + base58_encode(parcel.ED25519_MULTICODEC_PREFIX + public_key)
    return private_key, did


def signed_record(room: str, private_key: Ed25519PrivateKey, did: str, event: dict, nonce: int) -> dict:
    text = parcel.compact_json(event)
    signature = private_key.sign(f"{room}|{nonce}|{text}".encode())
    return {
        "seq": nonce,
        "ts": "2026-09-03T00:00:00Z",
        "from": did,
        "text": text,
        "nonce": nonce,
        "sig": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


class FakeOnboarding:
    def __init__(self, did: str):
        self.did = did

    def load_seed(self, identity: Path) -> str:
        return "ab" * 32

    def did_for(self, seed: str) -> str:
        return self.did

    def init_identity(self, identity: Path) -> str:
        return self.did

    def clean_message(self, text: str) -> str:
        return text

    def publish(self, room: str, text: str, identity: Path, kind: str) -> dict:
        return {
            "receipt_version": 1,
            "kind": kind,
            "verified_record": {
                "seq": 1,
                "ts": "2026-08-25T00:00:00Z",
                "from": self.did,
                "text": text,
                "nonce": 1,
            },
            "transient_locator": f"https://example/r/{room}/1",
        }


class ParcelCreationTests(unittest.TestCase):
    def test_private_capability_file_is_mode_600(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "parcel.json"
            with (
                mock.patch.object(parcel, "load_onboarding", return_value=FakeOnboarding(COORDINATOR_DID)),
                mock.patch.object(parcel, "write_note_if_absent"),
                mock.patch.object(
                    parcel,
                    "room_export",
                    return_value={"generation": "0", "sha256": "d" * 64, "records": []},
                ),
            ):
                path, created = parcel.create_parcel(
                    "Review",
                    "Return three improvements",
                    Path("identity.env"),
                    "https://chat.technocore-lab.com",
                    WORKER_DID,
                    output,
                )
            self.assertEqual(path, output)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertTrue(created["room"].startswith("p-parcel-"))
            self.assertEqual(created["room"], created["namespace"])
            self.assertNotIn(created["room"], created["task"]["instructions"])
            self.assertEqual(created["room_generation"], "0")

    def test_invalid_expected_worker_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            parcel, "load_onboarding", return_value=FakeOnboarding(COORDINATOR_DID)
        ):
            with self.assertRaisesRegex(parcel.ParcelError, "worker DID"):
                parcel.create_parcel(
                    "Review",
                    "Instructions",
                    Path("id"),
                    "https://example",
                    "bad",
                    Path(directory) / "p.json",
                )

    def test_worker_event_refuses_oversized_result_before_network(self) -> None:
        with self.assertRaisesRegex(parcel.ParcelError, "maximum"):
            parcel.worker_event(
                Path("missing-private-parcel.json"),
                Path("worker.env"),
                "result",
                "x" * (parcel.MAX_EVENT_BODY_CHARS + 1),
            )


class ClaimTests(unittest.TestCase):
    def private_parcel(self) -> dict:
        return {
            "parcel_version": 1,
            "task_id": "a" * 16,
            "service": "https://chat.technocore-lab.com",
            "room": "p-parcel-" + "b" * 20,
            "namespace": "p-parcel-" + "b" * 20,
            "task": {"title": "Review", "instructions": "Return improvements"},
            "coordinator_did": COORDINATOR_DID,
            "expected_worker_did": WORKER_DID,
            "room_generation": "0",
            "events": [],
        }

    def test_compare_and_set_claim_is_signed_by_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "parcel.json"
            parcel.write_private_json(path, self.private_parcel(), replace=False)
            with (
                mock.patch.object(parcel, "load_onboarding", return_value=FakeOnboarding(WORKER_DID)),
                mock.patch.object(parcel, "write_note_if_absent") as write_claim,
            ):
                claim = parcel.claim_parcel(path, Path("worker.env"), "worker")
            self.assertEqual(claim["worker_did"], WORKER_DID)
            write_claim.assert_called_once()
            saved = parcel.load_parcel(path)
            self.assertEqual(len(saved["events"]), 1)

    def test_losing_worker_cannot_reclaim_parcel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "parcel.json"
            value = self.private_parcel()
            value["expected_worker_did"] = None
            parcel.write_private_json(path, value, replace=False)
            with (
                mock.patch.object(parcel, "load_onboarding", return_value=FakeOnboarding(WORKER_DID)),
                mock.patch.object(parcel, "write_note_if_absent", side_effect=parcel.ClaimConflict("won")),
                mock.patch.object(parcel, "read_note", return_value={"worker_did": COORDINATOR_DID}),
                self.assertRaisesRegex(parcel.ParcelError, "already claimed"),
            ):
                parcel.claim_parcel(path, Path("worker.env"), "worker")


class SignatureVerificationTests(unittest.TestCase):
    def test_ed25519_signature_is_verified_against_sender_did(self) -> None:
        private_key, did = signing_identity()
        room = "p-parcel-" + "b" * 20
        record = signed_record(
            room,
            private_key,
            did,
            {"task_id": "a" * 16, "type": "result"},
            7,
        )

        self.assertTrue(parcel.verify_record_signature(room, record))
        record["text"] += " "
        self.assertFalse(parcel.verify_record_signature(room, record))

    def test_room_export_retains_generation_and_raw_hash(self) -> None:
        body = b'{"seq":1,"text":"first"}\n{"seq":2,"text":"second"}\n'
        private = ClaimTests().private_parcel()
        with mock.patch.object(
            parcel,
            "request_response",
            return_value=(body, {"X-Room-Generation": "3"}),
        ):
            exported = parcel.room_export(private)

        self.assertEqual(exported["generation"], "3")
        self.assertEqual(exported["sha256"], parcel.hashlib.sha256(body).hexdigest())
        self.assertEqual(len(exported["records"]), 2)

    def test_invalid_exported_signature_invalidates_parcel(self) -> None:
        coordinator_key, coordinator_did = signing_identity()
        worker_key, worker_did = signing_identity()
        private = ClaimTests().private_parcel()
        private["coordinator_did"] = coordinator_did
        private["expected_worker_did"] = worker_did
        private["room_generation"] = "4"
        task = {"task_id": private["task_id"], "type": "task"}
        result = {
            "task_id": private["task_id"],
            "type": "result",
            "worker_did": worker_did,
            "body": "done",
        }
        records = [
            signed_record(private["room"], coordinator_key, coordinator_did, task, 1),
            signed_record(private["room"], worker_key, worker_did, result, 2),
        ]
        records[1]["sig"] = "A" * 86
        with tempfile.TemporaryDirectory() as directory:
            private_path = Path(directory) / "parcel.json"
            parcel.write_private_json(private_path, private, replace=False)
            with (
                mock.patch.object(
                    parcel,
                    "room_export",
                    return_value={"generation": "4", "sha256": "d" * 64, "records": records},
                ),
                mock.patch.object(parcel, "read_note", return_value={"worker_did": worker_did}),
                mock.patch.object(parcel, "service_version", return_value="0.11.4"),
            ):
                verification = parcel.verify_parcel(private_path)

        self.assertFalse(verification["valid"])
        self.assertFalse(verification["signatures_valid"])
        self.assertIn("result event signature was invalid", verification["errors"])

    def test_expected_worker_pin_is_rechecked_during_verification(self) -> None:
        private = ClaimTests().private_parcel()
        exported = {"generation": "0", "sha256": "d" * 64, "records": []}
        with tempfile.TemporaryDirectory() as directory:
            private_path = Path(directory) / "parcel.json"
            parcel.write_private_json(private_path, private, replace=False)
            with (
                mock.patch.object(parcel, "room_export", return_value=exported),
                mock.patch.object(parcel, "read_note", return_value={"worker_did": COORDINATOR_DID}),
                mock.patch.object(parcel, "service_version", return_value="0.11.4"),
            ):
                verification = parcel.verify_parcel(private_path)

        self.assertIn("claim owner did not match expected worker", verification["errors"])


class PublicExportTests(unittest.TestCase):
    def test_export_does_not_leak_private_room_or_namespace(self) -> None:
        private = ClaimTests().private_parcel()
        with tempfile.TemporaryDirectory() as directory:
            parcel_path = Path(directory) / "parcel.json"
            output = Path(directory) / "public.json"
            parcel.write_private_json(parcel_path, private, replace=False)
            verification = {
                "coordinator_did": COORDINATOR_DID,
                "worker_did": WORKER_DID,
                "event_count": 3,
                "result_count": 1,
                "results": [{"event": {"type": "result", "body": "done"}}],
                "signature_count": 3,
                "signatures_valid": True,
                "room_generation": "4",
                "room_export_sha256": "d" * 64,
                "service_version": "0.11.4",
                "valid": True,
                "errors": [],
            }
            with mock.patch.object(parcel, "verify_parcel", return_value=verification):
                public = parcel.export_public(parcel_path, output)
            serialized = json.dumps(public)
            self.assertNotIn(private["room"], serialized)
            self.assertNotIn(private["namespace"], serialized)
            self.assertFalse(public["private_capability_recorded"])
            self.assertTrue(public["signatures_valid"])
            self.assertEqual(public["room_generation"], "4")
            self.assertNotIn('"sig"', serialized)
            self.assertEqual(public["expected_worker_did"], WORKER_DID)


class ClaudeAdapterTests(unittest.TestCase):
    def test_prompt_contains_task_but_not_private_capability(self) -> None:
        private = ClaimTests().private_parcel()
        with tempfile.TemporaryDirectory() as directory:
            parcel_path = Path(directory) / "parcel.json"
            prompt_path = Path(directory) / "prompt.json"
            parcel.write_private_json(parcel_path, private, replace=False)
            with (
                mock.patch.object(claude_adapter.parcel_module, "load_onboarding", return_value=FakeOnboarding(WORKER_DID)),
                mock.patch.object(
                    claude_adapter.parcel_module,
                    "claim_parcel",
                    return_value={"worker_did": WORKER_DID},
                ),
            ):
                claude_adapter.prepare(parcel_path, Path("worker.env"), prompt_path)
            prompt = prompt_path.read_text()
            self.assertIn("Return improvements", prompt)
            self.assertNotIn(private["room"], prompt)
            self.assertNotIn(private["namespace"], prompt)
            self.assertEqual(stat.S_IMODE(prompt_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
