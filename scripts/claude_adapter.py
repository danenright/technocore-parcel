#!/usr/bin/env python3
"""Bridge a private Technocore parcel to external Claude without exposing its DID seed."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("technocore_parcel", ROOT / "parcel.py")
if not SPEC or not SPEC.loader:
    raise RuntimeError("could not load parcel module")
parcel_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parcel_module)

DEFAULT_WORKER_IDENTITY = Path.home() / ".config" / "technocore" / "claude-parcel-worker.env"


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def prepare(parcel_path: Path, identity: Path, output: Path) -> dict:
    parcel = parcel_module.load_parcel(parcel_path)
    onboarding = parcel_module.load_onboarding(parcel["service"])
    worker_did = onboarding.init_identity(identity)
    expected = parcel.get("expected_worker_did")
    if expected and expected != worker_did:
        raise parcel_module.ParcelError("Claude adapter DID does not match expected worker")
    claim = parcel_module.claim_parcel(parcel_path, identity, "claude-code-adapter")
    prompt = {
        "task_id": parcel["task_id"],
        "title": parcel["task"]["title"],
        "instructions": parcel["task"]["instructions"],
        "worker_did": worker_did,
        "response_contract": {
            "format": "plain UTF-8 text",
            "max_characters": 2800,
            "rule": "Return only the completed task result. Do not include secrets or the private parcel capability.",
        },
    }
    if claim.get("claim_id"):
        prompt["claim_id"] = claim["claim_id"]
        prompt["checkpoint"] = claim.get("checkpoint")
    write_private(output, json.dumps(prompt, indent=2) + "\n")
    return claim


def submit(parcel_path: Path, identity: Path, response: Path, claim_id: str | None = None) -> dict:
    parcel = parcel_module.load_parcel(parcel_path)
    if parcel["parcel_version"] == 2 and (not claim_id or claim_id != parcel.get("claim_id")):
        raise parcel_module.ParcelError("submit requires the claim-id from this attempt's prepared prompt")
    body = response.read_text(encoding="utf-8").strip()
    if not body:
        raise parcel_module.ParcelError("Claude response is empty")
    return parcel_module.worker_event(parcel_path, identity, "result", body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", type=Path, default=DEFAULT_WORKER_IDENTITY)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("parcel", type=Path)
    prepare_parser.add_argument("--output", type=Path, required=True)
    submit_parser = subparsers.add_parser("submit")
    submit_parser.add_argument("parcel", type=Path)
    submit_parser.add_argument("--response", type=Path, required=True)
    submit_parser.add_argument("--claim-id", help="required for leased parcels; copy from the prepared prompt")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            claim = prepare(args.parcel, args.identity, args.output)
            print(f"claimed by: {claim['worker_did']}")
            print(f"Claude prompt: {args.output}")
            print("The prompt contains no room capability or private seed.")
        else:
            receipt = submit(args.parcel, args.identity, args.response, args.claim_id)
            print(f"result sequence: {receipt['verified_record']['seq']}")
        return 0
    except (FileExistsError, OSError, ValueError, parcel_module.ParcelError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
