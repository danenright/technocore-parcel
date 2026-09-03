#!/usr/bin/env python3
"""Coordinate one claimed task between agents through a private Technocore parcel."""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime
import hashlib
import importlib.util
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

DEFAULT_SERVICE = os.environ.get("TECHNOCORE_URL", "https://chat.technocore-lab.com").rstrip("/")
DEFAULT_IDENTITY = Path.home() / ".config" / "technocore" / "agent.env"
DEFAULT_PARCEL_DIR = Path.home() / ".local" / "state" / "technocore-parcel"
ONBOARDING_COMMIT = "2d9cfc6feca8e53d66c26cdc7d2ca14c3fa05dc7"
ONBOARDING_URL = (
    "https://raw.githubusercontent.com/danenright/technocore-contributor-onboarding/"
    f"{ONBOARDING_COMMIT}/technocore_onboard.py"
)
ONBOARDING_SHA256 = "5140ffe93cf45ab48a8d7dff2d4391c233a796e41e300233f9052dab618e6223"
ONBOARDING_CACHE = Path.home() / ".cache" / "technocore-parcel" / f"onboard-{ONBOARDING_COMMIT}.py"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
DID_RE = re.compile(r"^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$")
TASK_ID_RE = re.compile(r"^[0-9a-f]{16}$")
MAX_EVENT_BODY_CHARS = 2800
SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{86}$")
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ED25519_MULTICODEC_PREFIX = b"\xed\x01"


class ParcelError(RuntimeError):
    pass


class ClaimConflict(ParcelError):
    pass


def utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")


def request_response(url: str, payload: dict | None = None) -> tuple[bytes, object]:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = {"User-Agent": "technocore-parcel/0.2"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read(), response.headers
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace").strip()
        if error.code == 409:
            raise ClaimConflict(body) from error
        raise ParcelError(f"Technocore HTTP {error.code}: {body or error.reason}") from error


def request_bytes(url: str, payload: dict | None = None) -> bytes:
    body, _ = request_response(url, payload)
    return body


def ensure_onboarding_helper(path: Path = ONBOARDING_CACHE) -> Path:
    if path.exists():
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != ONBOARDING_SHA256:
            raise ParcelError(f"cached onboarding helper checksum mismatch: {actual}")
        return path
    request = urllib.request.Request(ONBOARDING_URL, headers={"User-Agent": "technocore-parcel/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read()
    actual = hashlib.sha256(content).hexdigest()
    if actual != ONBOARDING_SHA256:
        raise ParcelError(f"downloaded onboarding helper checksum mismatch: {actual}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
    return path


def load_onboarding(service: str):
    path = ensure_onboarding_helper()
    module_name = "technocore_onboard_" + hashlib.sha256(service.encode()).hexdigest()[:12]
    old_service = os.environ.get("TECHNOCORE_URL")
    os.environ["TECHNOCORE_URL"] = service
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if not spec or not spec.loader:
            raise ParcelError("could not load pinned onboarding helper")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if old_service is None:
            os.environ.pop("TECHNOCORE_URL", None)
        else:
            os.environ["TECHNOCORE_URL"] = old_service


def compact_json(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def note_url(parcel: dict, key: str) -> str:
    return f"{parcel['service']}/kv/{parcel['namespace']}/{key}"


def write_note_if_absent(parcel: dict, key: str, value: dict) -> None:
    request_bytes(note_url(parcel, key), {"value": compact_json(value), "if_absent": True})


def read_note(parcel: dict, key: str) -> dict | None:
    try:
        body = request_bytes(note_url(parcel, key)).decode("utf-8")
    except ParcelError as error:
        if "HTTP 404" in str(error):
            return None
        raise
    if "\n\n" not in body:
        raise ParcelError(f"note {key!r} returned an unexpected body")
    value = body.split("\n\n", 1)[1]
    value = value.split("\n# budget:", 1)[0].strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ParcelError(f"note {key!r} is not a parcel JSON object") from error
    if not isinstance(parsed, dict):
        raise ParcelError(f"note {key!r} is not an object")
    return parsed


def parcel_path(task_id: str) -> Path:
    return DEFAULT_PARCEL_DIR / f"parcel-{task_id}.json"


def write_private_json(path: Path, payload: dict, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if replace:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")


def load_parcel(path: Path) -> dict:
    try:
        parcel = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ParcelError(f"could not read parcel capability: {path}") from error
    required = {"parcel_version", "task_id", "service", "room", "namespace", "task", "coordinator_did", "events"}
    if not isinstance(parcel, dict) or not required.issubset(parcel):
        raise ParcelError("parcel capability is missing required fields")
    if parcel["parcel_version"] != 1 or not TASK_ID_RE.fullmatch(parcel["task_id"]):
        raise ParcelError("unsupported parcel version or task id")
    if not NAME_RE.fullmatch(parcel["room"]) or not parcel["room"].startswith("p-"):
        raise ParcelError("parcel room is not a private p- capability")
    if not NAME_RE.fullmatch(parcel["namespace"]) or not parcel["namespace"].startswith("p-"):
        raise ParcelError("parcel namespace is not private")
    if not DID_RE.fullmatch(parcel["coordinator_did"]):
        raise ParcelError("parcel coordinator DID is invalid")
    return parcel


def signed_event(parcel: dict, identity: Path, event: dict, kind: str) -> dict:
    onboarding = load_onboarding(parcel["service"])
    receipt = onboarding.publish(parcel["room"], compact_json(event), identity, kind)
    parcel["events"].append(receipt)
    return receipt


def create_parcel(
    title: str,
    instructions: str,
    identity: Path,
    service: str,
    expected_worker_did: str | None,
    output: Path | None,
) -> tuple[Path, dict]:
    task_id = secrets.token_hex(8)
    capability = secrets.token_hex(10)
    room = f"p-parcel-{capability}"
    namespace = f"p-parcel-{capability}"
    onboarding = load_onboarding(service)
    coordinator_seed = onboarding.load_seed(identity)
    coordinator_did = onboarding.did_for(coordinator_seed)
    if expected_worker_did and not DID_RE.fullmatch(expected_worker_did):
        raise ParcelError("expected worker DID is invalid")
    task = {
        "v": 1,
        "type": "task",
        "task_id": task_id,
        "title": title,
        "instructions": instructions,
        "coordinator_did": coordinator_did,
        "expected_worker_did": expected_worker_did,
        "created_at": utc_now(),
    }
    parcel = {
        "parcel_version": 1,
        "task_id": task_id,
        "service": service,
        "room": room,
        "namespace": namespace,
        "task": task,
        "coordinator_did": coordinator_did,
        "expected_worker_did": expected_worker_did,
        "events": [],
    }
    onboarding.clean_message(compact_json(task))
    write_note_if_absent(parcel, "task", task)
    signed_event(parcel, identity, task, "contribution")
    parcel["room_generation"] = room_export(parcel)["generation"]
    path = output or parcel_path(task_id)
    write_private_json(path, parcel, replace=False)
    return path, parcel


def claim_parcel(path: Path, identity: Path, worker_label: str) -> dict:
    parcel = load_parcel(path)
    onboarding = load_onboarding(parcel["service"])
    worker_did = onboarding.did_for(onboarding.load_seed(identity))
    expected = parcel.get("expected_worker_did")
    if expected and worker_did != expected:
        raise ParcelError("this worker DID is not the expected worker")
    claim = {
        "v": 1,
        "type": "claim",
        "task_id": parcel["task_id"],
        "worker_did": worker_did,
        "worker_label": worker_label,
        "claimed_at": utc_now(),
    }
    try:
        write_note_if_absent(parcel, "claim", claim)
    except ClaimConflict:
        current = read_note(parcel, "claim")
        if not current or current.get("worker_did") != worker_did:
            raise ParcelError(f"parcel already claimed by {current.get('worker_did') if current else 'unknown'}")
        claim = current
    if not any(event.get("verified_record", {}).get("text") == compact_json(claim) for event in parcel["events"]):
        signed_event(parcel, identity, claim, "contribution")
        write_private_json(path, parcel, replace=True)
    return claim


def require_claim(parcel: dict, worker_did: str) -> dict:
    claim = read_note(parcel, "claim")
    if not claim:
        raise ParcelError("parcel has not been claimed")
    if claim.get("worker_did") != worker_did:
        raise ParcelError("worker DID does not own the parcel claim")
    return claim


def worker_event(path: Path, identity: Path, event_type: str, body: str) -> dict:
    if len(body) > MAX_EVENT_BODY_CHARS:
        raise ParcelError(
            f"{event_type} body is {len(body)} characters; maximum is {MAX_EVENT_BODY_CHARS}"
        )
    parcel = load_parcel(path)
    onboarding = load_onboarding(parcel["service"])
    worker_did = onboarding.did_for(onboarding.load_seed(identity))
    require_claim(parcel, worker_did)
    event = {
        "v": 1,
        "type": event_type,
        "task_id": parcel["task_id"],
        "worker_did": worker_did,
        "body": body,
        "created_at": utc_now(),
    }
    receipt = signed_event(parcel, identity, event, "contribution")
    write_private_json(path, parcel, replace=True)
    return receipt


def base58_decode(value: str) -> bytes:
    if not value or any(character not in BASE58_ALPHABET for character in value):
        raise ValueError("invalid base58btc value")
    number = 0
    for character in value:
        number = number * 58 + BASE58_ALPHABET.index(character)
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\0" * (len(value) - len(value.lstrip("1"))) + decoded


def verify_record_signature(room: str, record: dict) -> bool:
    sender = record.get("from")
    signature = record.get("sig")
    nonce = record.get("nonce")
    text = record.get("text")
    if (
        not isinstance(sender, str)
        or not DID_RE.fullmatch(sender)
        or not isinstance(signature, str)
        or not SIGNATURE_RE.fullmatch(signature)
        or not isinstance(nonce, (str, int))
        or isinstance(nonce, bool)
        or not isinstance(text, str)
    ):
        return False
    try:
        multikey = base58_decode(sender.removeprefix("did:key:z"))
        if len(multikey) != 34 or not multikey.startswith(ED25519_MULTICODEC_PREFIX):
            return False
        signature_bytes = base64.urlsafe_b64decode(signature + "==")
        if len(signature_bytes) != 64:
            return False
        payload = f"{room}|{nonce}|{text}".encode()
        Ed25519PublicKey.from_public_bytes(multikey[2:]).verify(signature_bytes, payload)
    except (binascii.Error, InvalidSignature, ValueError):
        return False
    return True


def room_export(parcel: dict) -> dict:
    url = f"{parcel['service']}/r/{parcel['room']}/export?n={time_ns()}"
    body, headers = request_response(url)
    generation = headers.get("X-Room-Generation")
    if not generation:
        raise ParcelError("Technocore room export omitted X-Room-Generation")
    records = []
    for line_number, line in enumerate(body.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ParcelError(f"Technocore room export line {line_number} is not JSON") from error
        if not isinstance(record, dict):
            raise ParcelError(f"Technocore room export line {line_number} is not an object")
        records.append(record)
    return {
        "generation": generation,
        "sha256": hashlib.sha256(body).hexdigest(),
        "records": records,
    }


def parcel_events(parcel: dict, records: list[dict]) -> list[dict]:
    events = []
    for record in records:
        try:
            event = json.loads(record.get("text", ""))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("task_id") != parcel["task_id"]:
            continue
        events.append({"record": record, "event": event})
    return events


def room_events(parcel: dict) -> list[dict]:
    return parcel_events(parcel, room_export(parcel)["records"])


def time_ns() -> int:
    return int(datetime.datetime.now(datetime.UTC).timestamp() * 1_000_000_000)


def service_version(service: str) -> str:
    payload = json.loads(request_bytes(f"{service}/.well-known/agent.json"))
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version:
        raise ParcelError("Technocore manifest omitted version")
    return version


def verify_parcel(path: Path) -> dict:
    parcel = load_parcel(path)
    first_export = room_export(parcel)
    claim = read_note(parcel, "claim")
    exported = room_export(parcel)
    events = parcel_events(parcel, exported["records"])
    errors = []
    recorded_generation = parcel.get("room_generation")
    if recorded_generation is None:
        errors.append("parcel did not record its room generation")
    elif recorded_generation != exported["generation"]:
        errors.append("room generation did not match parcel creation")
    if first_export["generation"] != exported["generation"]:
        errors.append("room generation changed during verification")
    expected_worker_did = parcel.get("expected_worker_did")
    if claim and expected_worker_did and claim.get("worker_did") != expected_worker_did:
        errors.append("claim owner did not match expected worker")
    results = []
    signature_count = 0
    task_found = False
    signatures_valid = bool(events)
    for item in events:
        event = item["event"]
        record = item["record"]
        sender = record.get("from")
        event_type = event.get("type")
        signature_count += 1
        signature_valid = verify_record_signature(parcel["room"], record)
        signatures_valid = signatures_valid and signature_valid
        if not signature_valid:
            errors.append(f"{event_type or 'unknown'} event signature was invalid")
        if event_type == "task":
            task_found = True
            if sender != parcel["coordinator_did"]:
                errors.append("task announcement sender did not match coordinator")
        if event_type in {"claim", "progress", "result"}:
            if event.get("worker_did") != sender:
                errors.append(f"{event_type} sender did not match worker_did")
            if claim and sender != claim.get("worker_did"):
                errors.append(f"{event_type} sender did not own claim")
        if event_type == "result":
            results.append(item)
    if not task_found:
        errors.append("parcel had no task announcement")
    if claim is None:
        errors.append("parcel had no claim")
    if not results:
        errors.append("parcel had no result")
    return {
        "parcel_version": 1,
        "task_id": parcel["task_id"],
        "coordinator_did": parcel["coordinator_did"],
        "worker_did": claim.get("worker_did") if claim else None,
        "claimed": claim is not None,
        "event_count": len(events),
        "result_count": len(results),
        "results": results,
        "signature_count": signature_count,
        "signatures_valid": signatures_valid,
        "room_generation": exported["generation"],
        "room_export_sha256": exported["sha256"],
        "service_version": service_version(parcel["service"]),
        "errors": errors,
        "valid": not errors and bool(results),
    }


def export_public(path: Path, output: Path) -> dict:
    verification = verify_parcel(path)
    parcel = load_parcel(path)
    public = {
        "parcel_version": 1,
        "task_id": parcel["task_id"],
        "title": parcel["task"]["title"],
        "coordinator_did": verification["coordinator_did"],
        "expected_worker_did": parcel.get("expected_worker_did"),
        "worker_did": verification["worker_did"],
        "event_count": verification["event_count"],
        "result_count": verification["result_count"],
        "result_sha256": [
            hashlib.sha256(compact_json(item["event"]).encode()).hexdigest()
            for item in verification["results"]
        ],
        "signature_count": verification["signature_count"],
        "signatures_valid": verification["signatures_valid"],
        "room_generation": verification["room_generation"],
        "room_export_sha256": verification["room_export_sha256"],
        "service_version": verification["service_version"],
        "valid": verification["valid"],
        "errors": verification["errors"],
        "service": parcel["service"],
        "private_capability_recorded": False,
        "exported_at": utc_now(),
    }
    if parcel["room"] in compact_json(public) or parcel["namespace"] in compact_json(public):
        raise ParcelError("private parcel capability leaked into public export")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(public, stream, indent=2)
        stream.write("\n")
    return public


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", type=Path, default=DEFAULT_IDENTITY)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("identity")
    create = subparsers.add_parser("create")
    create.add_argument("--title", required=True)
    create.add_argument("--instructions", required=True)
    create.add_argument("--expected-worker-did")
    create.add_argument("--output", type=Path)
    claim = subparsers.add_parser("claim")
    claim.add_argument("parcel", type=Path)
    claim.add_argument("--worker-label", required=True)
    progress = subparsers.add_parser("progress")
    progress.add_argument("parcel", type=Path)
    progress.add_argument("--message", required=True)
    result = subparsers.add_parser("result")
    result.add_argument("parcel", type=Path)
    result_group = result.add_mutually_exclusive_group(required=True)
    result_group.add_argument("--text")
    result_group.add_argument("--file", type=Path)
    watch = subparsers.add_parser("watch")
    watch.add_argument("parcel", type=Path)
    export = subparsers.add_parser("export")
    export.add_argument("parcel", type=Path)
    export.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "identity":
            onboarding = load_onboarding(args.service)
            print(onboarding.init_identity(args.identity))
        elif args.command == "create":
            path, parcel = create_parcel(
                args.title,
                args.instructions,
                args.identity,
                args.service.rstrip("/"),
                args.expected_worker_did,
                args.output,
            )
            print(f"parcel: {path}")
            print(f"task_id: {parcel['task_id']}")
            print("capability: private; share the parcel file only with the intended worker adapter")
        elif args.command == "claim":
            claim = claim_parcel(args.parcel, args.identity, args.worker_label)
            print(f"claimed by: {claim['worker_did']}")
        elif args.command == "progress":
            worker_event(args.parcel, args.identity, "progress", args.message)
            print("progress posted")
        elif args.command == "result":
            body = args.text if args.text is not None else args.file.read_text(encoding="utf-8")
            receipt = worker_event(args.parcel, args.identity, "result", body)
            print(f"result sequence: {receipt['verified_record']['seq']}")
        elif args.command == "watch":
            print(json.dumps(verify_parcel(args.parcel), indent=2))
        else:
            public = export_public(args.parcel, args.output)
            print(f"public evidence: {args.output}")
            print(f"valid: {public['valid']}")
        return 0
    except (ClaimConflict, FileExistsError, OSError, ParcelError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
