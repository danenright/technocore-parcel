#!/usr/bin/env python3
"""Fund a Technocore DID on the FLOP network and spend it on real inference tasks."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("technocore_parcel", ROOT / "parcel.py")
if not SPEC or not SPEC.loader:
    raise RuntimeError("could not load parcel module")
parcel_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parcel_module)

DEFAULT_IDENTITY = parcel_module.DEFAULT_IDENTITY
DEFAULT_STATE_DIR = parcel_module.DEFAULT_PARCEL_DIR / "flop-agent"
USER_AGENT = "technocore-parcel-flop-agent/0.1"
SEED_RE = re.compile(r"^[0-9a-f]{64}$")
GENESIS_RE = re.compile(r"^0x[0-9a-f]{64}$")
NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
SS58_PREFIX = 42
SS58_CHECKSUM_PREFIX = b"SS58PRE"
FAUCET_COOLDOWN_SECONDS = 86_400
CREDENTIAL_MARGIN_SECONDS = 120
FUND_SETTLE_SECONDS = 20
MAX_PROMPT_CHARS = 16_000
TASK_SUFFIXES = (".json",)


class AgentError(parcel_module.ParcelError):
    pass


@dataclass(frozen=True)
class Network:
    name: str
    faucet_url: str
    gateway_url: str
    expected_genesis: str | None = None


NETWORKS = {
    "devnet": Network("devnet", "https://faucet.flop.network", "https://api.flop.network/api/v1"),
}


@dataclass
class AgentContext:
    identity: Path
    network: Network
    state_dir: Path
    key: Ed25519PrivateKey
    did: str
    address: str
    genesis: str = ""
    ledger: list[dict] = field(default_factory=list)

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / self.network.name / "ledger.jsonl"

    @property
    def credential_path(self) -> Path:
        return self.state_dir / self.network.name / "credential.json"

    @property
    def identity_pin_path(self) -> Path:
        return self.state_dir / self.network.name / "identity.json"

    @property
    def results_dir(self) -> Path:
        return self.state_dir / self.network.name / "results"


def utc_now() -> str:
    return parcel_module.utc_now()


def now_ms() -> int:
    return int(time.time() * 1000)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def base58_encode(value: bytes) -> str:
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = parcel_module.BASE58_ALPHABET[remainder] + encoded
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + encoded


def ss58_checksum(body: bytes) -> bytes:
    return hashlib.blake2b(SS58_CHECKSUM_PREFIX + body, digest_size=64).digest()[:2]


def ss58_address(public_key: bytes, prefix: int = SS58_PREFIX) -> str:
    if len(public_key) != 32 or not 0 <= prefix < 64:
        raise AgentError("SS58 encoding needs a 32-byte key and a one-byte prefix")
    body = bytes([prefix]) + public_key
    return base58_encode(body + ss58_checksum(body))


def ss58_public_key(address: str) -> bytes:
    try:
        raw = parcel_module.base58_decode(address)
    except ValueError as error:
        raise AgentError("address is not base58") from error
    if len(raw) != 35 or raw[0] >= 64:
        raise AgentError("address is not a one-byte-prefix SS58 account")
    if ss58_checksum(raw[:33]) != raw[33:]:
        raise AgentError("address SS58 checksum is invalid")
    return raw[1:33]


def load_signing_key(identity: Path) -> Ed25519PrivateKey:
    try:
        line = identity.read_text(encoding="utf-8").strip()
    except FileNotFoundError as error:
        raise AgentError(f"identity not found at {identity}") from error
    seed = line.removeprefix("SIGN_SEED=") if line.startswith("SIGN_SEED=") else ""
    if not SEED_RE.fullmatch(seed):
        raise AgentError(f"identity at {identity} is not a 64-hex Technocore seed")
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))


def raw_public_key(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def did_for_public_key(public_key: bytes) -> str:
    return "did:key:z" + base58_encode(parcel_module.ED25519_MULTICODEC_PREFIX + public_key)


def http_json(
    method: str,
    url: str,
    payload: dict | None = None,
    bearer: str | None = None,
    timeout: int = 60,
) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise AgentError(f"{method} {url} failed: {error}") from error
    try:
        parsed = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = {"error": "non_json_response", "message": body[:200].decode("utf-8", errors="replace")}
    if not isinstance(parsed, dict):
        parsed = {"value": parsed}
    return status, parsed


def error_text(status: int, body: dict) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        error = error.get("code") or error.get("message")
    message = body.get("message")
    return f"HTTP {status}: {error or 'error'}" + (f" ({message})" if message else "")


def write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    parcel_module.write_private_json(path, payload, replace=True)


def load_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise AgentError(f"ledger line {line_number} is not JSON") from error
    return entries


def append_ledger(ctx: AgentContext, entry: dict) -> dict:
    record = {"ts": utc_now(), "network": ctx.network.name, "genesis": ctx.genesis, "address": ctx.address}
    record.update(entry)
    serialized = json.dumps(record, sort_keys=True, separators=(",", ":"))
    stored_key = stored_credential_key(ctx)
    if "key" in record or (stored_key and stored_key in serialized):
        raise AgentError("refusing to write a credential into the ledger")
    ctx.ledger_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(ctx.ledger_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(serialized + "\n")
    ctx.ledger.append(record)
    return record


def last_success(ctx: AgentContext, kind: str) -> dict | None:
    for entry in reversed(ctx.ledger):
        if entry.get("kind") == kind and entry.get("ok") and entry.get("genesis") == ctx.genesis:
            return entry
    return None


def parse_ts(value: str) -> float:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def chain_info(network: Network) -> dict:
    status, body = http_json("GET", f"{network.faucet_url}/chain")
    if status != 200:
        raise AgentError(f"faucet chain info unavailable: {error_text(status, body)}")
    genesis = body.get("genesisHash")
    if not isinstance(genesis, str) or not GENESIS_RE.fullmatch(genesis):
        raise AgentError("faucet chain info has no valid genesis hash")
    if body.get("network") != network.name:
        raise AgentError(f"faucet serves {body.get('network')!r}, not {network.name!r}")
    if network.expected_genesis and genesis != network.expected_genesis:
        raise AgentError("faucet genesis does not match the pinned network genesis")
    return body


def pin_identity(ctx: AgentContext, accept_new_genesis: bool) -> None:
    pin = {"did": ctx.did, "address": ctx.address, "genesis": ctx.genesis}
    if not ctx.identity_pin_path.exists():
        write_private_json(ctx.identity_pin_path, pin)
        return
    current = json.loads(ctx.identity_pin_path.read_text(encoding="utf-8"))
    if current.get("did") != ctx.did or current.get("address") != ctx.address:
        raise AgentError(
            "this state directory is pinned to a different identity; one participant uses one DID and one account"
        )
    if current.get("genesis") != ctx.genesis:
        if not accept_new_genesis:
            raise AgentError("network genesis changed since the last run; pass --accept-new-genesis after checking")
        write_private_json(ctx.identity_pin_path, pin)


def open_context(
    identity: Path,
    network: Network,
    state_dir: Path,
    address: str | None,
    accept_new_genesis: bool,
) -> AgentContext:
    key = load_signing_key(identity)
    public_key = raw_public_key(key)
    did = did_for_public_key(public_key)
    if address:
        ss58_public_key(address)
    ctx = AgentContext(identity, network, state_dir, key, did, address or ss58_address(public_key))
    ctx.genesis = chain_info(network)["genesisHash"]
    ctx.ledger = load_ledger(ctx.ledger_path)
    pin_identity(ctx, accept_new_genesis)
    return ctx


def fund(ctx: AgentContext, tier: str = "quick", provider: str | None = None, force: bool = False) -> dict:
    previous = last_success(ctx, "fund")
    if previous and not force:
        elapsed = time.time() - parse_ts(previous["ts"])
        if elapsed < FAUCET_COOLDOWN_SECONDS:
            return {"kind": "fund", "ok": False, "skipped": "cooldown", "retry_after_s": int(FAUCET_COOLDOWN_SECONDS - elapsed)}
    payload: dict = {"address": ctx.address}
    if tier == "linked":
        if provider not in {"brightid", "self"}:
            raise AgentError("a linked grant needs --provider brightid or self, verified by a human first")
        payload.update({"tier": "linked", "provider": provider})
    status, body = http_json("POST", f"{ctx.network.faucet_url}/fund", payload)
    ok = 200 <= status < 300 and body.get("success") is True
    entry = {"kind": "fund", "ok": ok, "tier": tier}
    if ok:
        entry.update({"amount_flop": body.get("amount"), "tx_hash": body.get("txHash"), "granted_tier": body.get("tier")})
    else:
        entry["error"] = error_text(status, body)
    return append_ledger(ctx, entry)


def verify_challenge(message: str, address: str, genesis: str, gateway_url: str, expires_at: int) -> None:
    lines = message.split("\n")
    host = urllib.parse.urlsplit(gateway_url).hostname or ""
    expected_head = f"{host} wants you to enroll a FLOP devnet gateway key with your account:"
    if len(lines) not in {8, 9} or lines[0] != expected_head or lines[1] != address or lines[2] != "":
        raise AgentError("enrollment challenge does not name this gateway and account")
    fields = {}
    for line in lines[3:]:
        name, separator, value = line.partition(": ")
        if not separator or name in fields:
            raise AgentError("enrollment challenge has an unexpected line")
        fields[name] = value
    allowed = {"Action", "Purpose", "Genesis", "Nonce", "Issued At", "Expiration Time"}
    if not set(fields) <= allowed or not {"Action", "Genesis", "Nonce", "Expiration Time"} <= set(fields):
        raise AgentError("enrollment challenge has unexpected fields")
    if not re.fullmatch(r"flop-devnet-enroll/v[12]", fields["Action"]):
        raise AgentError("enrollment challenge action is not a devnet gateway enrollment")
    if fields["Genesis"] != genesis:
        raise AgentError("enrollment challenge genesis does not match the faucet chain")
    if not NONCE_RE.fullmatch(fields["Nonce"]):
        raise AgentError("enrollment challenge nonce is malformed")
    if expires_at <= now_ms():
        raise AgentError("enrollment challenge has already expired")


def read_credential(ctx: AgentContext) -> dict | None:
    if not ctx.credential_path.exists():
        return None
    credential = json.loads(ctx.credential_path.read_text(encoding="utf-8"))
    return credential if isinstance(credential, dict) else None


def stored_credential_key(ctx: AgentContext) -> str | None:
    credential = read_credential(ctx)
    key = credential.get("key") if credential else None
    return key if isinstance(key, str) and key else None


def load_credential(ctx: AgentContext) -> dict | None:
    credential = read_credential(ctx)
    if not credential or credential.get("address") != ctx.address or credential.get("genesis") != ctx.genesis:
        return None
    if int(credential.get("expires_at") or 0) <= now_ms() + CREDENTIAL_MARGIN_SECONDS * 1000:
        return None
    return credential


def enroll(ctx: AgentContext) -> dict:
    query = urllib.parse.urlencode({"address": ctx.address})
    status, challenge = http_json("GET", f"{ctx.network.gateway_url}/enroll/wallet/challenge?{query}")
    if status != 200:
        raise AgentError(f"enrollment challenge refused: {error_text(status, challenge)}")
    message = challenge.get("message")
    expires_at = challenge.get("expires_at")
    if not isinstance(message, str) or not isinstance(expires_at, int):
        raise AgentError("enrollment challenge is malformed")
    verify_challenge(message, ctx.address, ctx.genesis, ctx.network.gateway_url, expires_at)
    signature = "0x" + ctx.key.sign(message.encode("utf-8")).hex()
    status, body = http_json(
        "POST",
        f"{ctx.network.gateway_url}/enroll/wallet",
        {"address": ctx.address, "message": message, "signature": signature},
    )
    if status not in {200, 201} or not isinstance(body.get("key"), str):
        append_ledger(ctx, {"kind": "enroll", "ok": False, "error": error_text(status, body)})
        raise AgentError(f"enrollment refused: {error_text(status, body)}")
    credential = {
        "address": ctx.address,
        "genesis": ctx.genesis,
        "key": body["key"],
        "model": body.get("model"),
        "remaining_quota": body.get("remaining_quota"),
        "expires_at": body.get("expires_at"),
    }
    write_private_json(ctx.credential_path, credential)
    append_ledger(
        ctx,
        {
            "kind": "enroll",
            "ok": True,
            "model": credential["model"],
            "remaining_quota": credential["remaining_quota"],
            "expires_at": credential["expires_at"],
            "rotated": status == 200,
        },
    )
    return credential


def ensure_credential(ctx: AgentContext) -> dict:
    return load_credential(ctx) or enroll(ctx)


class GatewayBackend:
    """Brokered inference through the FLOP gateway; the gateway, not this account, opens the channel."""

    name = "gateway"

    def __init__(self, max_tokens: int, max_cost_flop: float, excluded_miners: set[str]):
        self.max_tokens = max_tokens
        self.max_cost_flop = max_cost_flop
        self.excluded_miners = excluded_miners

    def complete(self, ctx: AgentContext, prompt: str) -> dict:
        credential = ensure_credential(ctx)
        payload = {
            "model": credential["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "max_cost_flop": self.max_cost_flop,
            "stream": False,
        }
        status, body = http_json(
            "POST", f"{ctx.network.gateway_url}/chat/completions", payload, credential["key"], timeout=300
        )
        if status != 200:
            raise AgentError(f"gateway inference failed: {error_text(status, body)}")
        choices = body.get("choices") or []
        reply = choices[0].get("message", {}).get("content") if choices and isinstance(choices[0], dict) else None
        if not isinstance(reply, str) or not reply.strip():
            raise AgentError("gateway returned no completion text")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        receipt = body.get("x_flop_receipt") if isinstance(body.get("x_flop_receipt"), dict) else None
        miner = (receipt or {}).get("miner") or (receipt or {}).get("miner_address")
        if miner and miner in self.excluded_miners:
            raise AgentError("gateway routed the request to an excluded miner; its spend would not be independent")
        _, quota = http_json("GET", f"{ctx.network.gateway_url}/keys/me", bearer=credential["key"])
        return {
            "backend": self.name,
            "reply": reply,
            "request_id": body.get("id"),
            "model": body.get("model"),
            "cost_flop": usage.get("cost_flop"),
            "gflops_used": usage.get("gflops_used"),
            "completion_tokens": usage.get("completion_tokens"),
            "miner": miner,
            "receipt": receipt,
            "remaining_quota": quota.get("remaining_quota"),
            "settlement": "brokered",
        }


class CliBackend:
    """Direct session from this account's own wallet through the operator-supplied `flop` CLI."""

    name = "cli"
    REPLY_KEYS = ("reply", "output", "text", "content")
    MINER_KEYS = ("miner", "serving_miner", "servingMiner")
    CHANNEL_KEYS = ("channel", "channel_id", "channelId")
    SETTLEMENT_KEYS = ("settlement_tx", "settlementTx", "settlement", "tx_hash", "txHash")
    COST_KEYS = ("cost_flop", "paid_flop", "payable_flop", "minerPaid")
    TURN_KEYS = ("verified_turns", "verifiedTurns", "turns")

    def __init__(
        self,
        command: str,
        miner: str,
        model: str,
        max_cost_flop: float,
        keystore: Path,
        password_env: str,
        via: str,
        endpoint: str | None,
        excluded_miners: set[str],
        timeout: int = 900,
    ):
        if not command:
            raise AgentError("the cli backend needs --flop-cli or $FLOP_CLI (the operator-supplied CLI command)")
        ss58_public_key(miner)
        if miner in excluded_miners:
            raise AgentError("refusing to buy from an excluded miner; spend to your own miner is not credited")
        self.command = shlex.split(command)
        self.miner = miner
        self.model = model
        self.max_cost_flop = max_cost_flop
        self.keystore = keystore
        self.password_env = password_env
        self.via = via
        self.endpoint = endpoint
        self.excluded_miners = excluded_miners
        self.timeout = timeout

    def arguments(self, network: str, prompt: str) -> list[str]:
        arguments = [
            *self.command,
            "--network", network,
            "--keystore", str(self.keystore),
            "--password-env", self.password_env,
            "session", "run",
            "--miner", self.miner,
            "--model", self.model,
            "--prompt", prompt,
            "--max-flop", f"{self.max_cost_flop:g}",
            "--via", self.via,
            "--json",
        ]
        if self.endpoint:
            arguments += ["--endpoint", self.endpoint]
        return arguments

    @staticmethod
    def first(result: dict, keys: tuple[str, ...]):
        return next((result[key] for key in keys if result.get(key) not in (None, "")), None)

    def complete(self, ctx: AgentContext, prompt: str) -> dict:
        try:
            completed = subprocess.run(
                self.arguments(ctx.network.name, prompt),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AgentError(f"flop CLI did not complete: {error}") from error
        if completed.returncode != 0:
            raise AgentError(f"flop CLI exited {completed.returncode}: {completed.stderr.strip()[-400:]}")
        result = None
        for line in reversed(completed.stdout.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                result = candidate
                break
        if result is None:
            raise AgentError("flop CLI printed no JSON result")
        reply = self.first(result, self.REPLY_KEYS)
        channel = self.first(result, self.CHANNEL_KEYS)
        if not isinstance(reply, str) or channel is None:
            raise AgentError(f"unrecognised flop CLI result fields: {sorted(result)}")
        miner = self.first(result, self.MINER_KEYS) or self.miner
        if miner != self.miner or miner in self.excluded_miners:
            raise AgentError("flop CLI reports a different or excluded serving miner")
        return {
            "backend": self.name,
            "reply": reply,
            "request_id": str(channel),
            "model": self.model,
            "cost_flop": self.first(result, self.COST_KEYS),
            "gflops_used": result.get("gflops_used"),
            "miner": miner,
            "channel": channel,
            "settlement_tx": self.first(result, self.SETTLEMENT_KEYS),
            "verified_turns": self.first(result, self.TURN_KEYS),
            "settlement": "direct",
        }


def load_task(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AgentError(f"task {path.name} is not readable JSON") from error
    if not isinstance(value, dict):
        raise AgentError(f"task {path.name} is not an object")
    if "parcel_version" in value:
        parcel = parcel_module.load_parcel(path)
        prompt = f"{parcel['task']['title']}\n\n{parcel['task']['instructions']}"
        return {"kind": "parcel", "path": path, "title": parcel["task"]["title"], "prompt": prompt}
    title, prompt = value.get("title"), value.get("prompt")
    if not isinstance(title, str) or not isinstance(prompt, str) or not prompt.strip():
        raise AgentError(f"task {path.name} needs string title and prompt fields")
    return {"kind": "prompt", "path": path, "title": title, "prompt": prompt}


def run_task(ctx: AgentContext, backend, task: dict) -> dict:
    if len(task["prompt"]) > MAX_PROMPT_CHARS:
        raise AgentError(f"task prompt exceeds {MAX_PROMPT_CHARS} characters")
    if task["kind"] == "parcel":
        parcel_module.claim_parcel(task["path"], ctx.identity, "flop-agent")
    spent = backend.complete(ctx, task["prompt"])
    reply = spent.pop("reply")
    truncated = False
    if task["kind"] == "parcel":
        body = reply
        if len(body) > parcel_module.MAX_EVENT_BODY_CHARS:
            body = body[: parcel_module.MAX_EVENT_BODY_CHARS - 14] + "\n[truncated]"
            truncated = True
        receipt = parcel_module.worker_event(task["path"], ctx.identity, "result", body)
        spent["parcel_result_seq"] = receipt["verified_record"]["seq"]
    result_path = ctx.results_dir / f"{int(time.time())}-{sha256_text(task['prompt'])[:12]}.json"
    write_private_json(result_path, {"title": task["title"], "prompt": task["prompt"], "reply": reply, "spend": spent})
    return append_ledger(
        ctx,
        {
            "kind": "spend",
            "ok": True,
            "task_kind": task["kind"],
            "title_sha256": sha256_text(task["title"]),
            "prompt_sha256": sha256_text(task["prompt"]),
            "reply_sha256": sha256_text(reply),
            "reply_chars": len(reply),
            "reply_truncated": truncated,
            **spent,
        },
    )


def queue_tasks(queue: Path) -> list[Path]:
    if not queue.is_dir():
        raise AgentError(f"task queue {queue} is not a directory")
    return sorted(path for path in queue.iterdir() if path.is_file() and path.suffix in TASK_SUFFIXES)


def settle_task_file(path: Path, outcome: str) -> None:
    target = path.parent / outcome
    target.mkdir(exist_ok=True, mode=0o700)
    path.replace(target / path.name)


def run_cycle(ctx: AgentContext, backend, queue: Path, max_tasks: int, budget_flop: float, fund_tier: str) -> dict:
    summary = {"funded": None, "spent": 0, "spent_flop": 0.0, "failed": 0, "remaining": 0}
    if fund_tier != "none":
        summary["funded"] = fund(ctx, fund_tier)
        if summary["funded"].get("ok"):
            time.sleep(FUND_SETTLE_SECONDS)
    for path in queue_tasks(queue):
        if summary["spent"] >= max_tasks:
            break
        if summary["spent_flop"] + backend.max_cost_flop > budget_flop:
            break
        try:
            task = load_task(path)
            entry = run_task(ctx, backend, task)
        except parcel_module.ParcelError as error:
            append_ledger(ctx, {"kind": "spend", "ok": False, "task_file": path.name, "error": str(error)[:400]})
            summary["failed"] += 1
            if "already claimed" in str(error):
                settle_task_file(path, "skipped")
            continue
        settle_task_file(path, "done")
        summary["spent"] += 1
        summary["spent_flop"] += float(entry.get("cost_flop") or backend.max_cost_flop)
    summary["remaining"] = len(queue_tasks(queue))
    return summary


PUBLIC_SPEND_FIELDS = (
    "ts", "network", "genesis", "address", "backend", "settlement", "model", "miner", "request_id",
    "channel", "settlement_tx", "verified_turns", "cost_flop", "gflops_used", "completion_tokens",
    "task_kind", "prompt_sha256", "reply_sha256", "parcel_result_seq",
)


def export_evidence(ctx: AgentContext, output: Path) -> dict:
    spends = [
        {name: entry[name] for name in PUBLIC_SPEND_FIELDS if entry.get(name) is not None}
        for entry in ctx.ledger
        if entry.get("kind") == "spend" and entry.get("ok")
    ]
    funds = [
        {name: entry.get(name) for name in ("ts", "tier", "amount_flop", "tx_hash")}
        for entry in ctx.ledger
        if entry.get("kind") == "fund" and entry.get("ok")
    ]
    evidence = {
        "evidence_version": 1,
        "did": ctx.did,
        "address": ctx.address,
        "network": ctx.network.name,
        "genesis": ctx.genesis,
        "fund_count": len(funds),
        "funds": funds,
        "spend_count": len(spends),
        "spent_flop": sum(float(item.get("cost_flop") or 0) for item in spends),
        "spends": spends,
        "exported_at": utc_now(),
    }
    serialized = json.dumps(evidence)
    stored_key = stored_credential_key(ctx)
    if stored_key and stored_key in serialized:
        raise AgentError("gateway credential leaked into public evidence")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(evidence, stream, indent=2)
        stream.write("\n")
    return evidence


def resolve_network(args: argparse.Namespace) -> Network:
    if args.network in NETWORKS and not (args.faucet_url or args.gateway_url):
        base = NETWORKS[args.network]
        return Network(base.name, base.faucet_url, base.gateway_url, args.expected_genesis or base.expected_genesis)
    if not (args.faucet_url and args.gateway_url and args.expected_genesis):
        raise AgentError(
            f"{args.network} has no published endpoints yet; pass --faucet-url, --gateway-url and --expected-genesis"
        )
    if not GENESIS_RE.fullmatch(args.expected_genesis):
        raise AgentError("--expected-genesis must be a 0x-prefixed 32-byte hex hash")
    return Network(args.network, args.faucet_url.rstrip("/"), args.gateway_url.rstrip("/"), args.expected_genesis)


def build_backend(args: argparse.Namespace):
    excluded = set(args.exclude_miner or [])
    for miner in excluded:
        ss58_public_key(miner)
    if args.backend == "gateway":
        return GatewayBackend(args.max_tokens, args.max_cost_flop, excluded)
    if not args.miner or not args.model:
        raise AgentError("the cli backend needs --miner and --model")
    return CliBackend(
        args.flop_cli or os.environ.get("FLOP_CLI", ""),
        args.miner,
        args.model,
        args.max_cost_flop,
        args.keystore,
        args.password_env,
        args.via,
        args.endpoint,
        excluded,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", type=Path, default=DEFAULT_IDENTITY)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--network", default="devnet")
    parser.add_argument("--faucet-url")
    parser.add_argument("--gateway-url")
    parser.add_argument("--expected-genesis")
    parser.add_argument("--address", help="SS58 account to fund instead of the DID-derived account")
    parser.add_argument("--accept-new-genesis", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("address")
    subparsers.add_parser("status")
    fund_parser = subparsers.add_parser("fund")
    fund_parser.add_argument("--tier", choices=["quick", "linked"], default="quick")
    fund_parser.add_argument("--provider", choices=["brightid", "self"])
    fund_parser.add_argument("--force", action="store_true")
    subparsers.add_parser("enroll")
    for name in ("spend", "run"):
        command = subparsers.add_parser(name)
        command.add_argument("--backend", choices=["gateway", "cli"], default="gateway")
        command.add_argument("--max-tokens", type=int, default=512)
        command.add_argument("--max-cost-flop", type=float, default=1.0)
        command.add_argument("--exclude-miner", action="append", help="SS58 miner under your control; repeatable")
        command.add_argument("--flop-cli", help="operator-supplied CLI command; defaults to $FLOP_CLI")
        command.add_argument("--miner")
        command.add_argument("--model")
        command.add_argument("--keystore", type=Path, default=Path("agent.json"))
        command.add_argument("--password-env", default="FLOP_KEY_PASSWORD")
        command.add_argument("--via", choices=["auto", "offer", "force"], default="auto")
        command.add_argument("--endpoint")
    spend = subparsers.choices["spend"]
    source = spend.add_mutually_exclusive_group(required=True)
    source.add_argument("--task", type=Path, help="task JSON with title and prompt, or a private parcel file")
    source.add_argument("--prompt")
    run = subparsers.choices["run"]
    run.add_argument("--queue", type=Path, required=True)
    run.add_argument("--max-tasks", type=int, default=5)
    run.add_argument("--budget-flop", type=float, default=20.0)
    run.add_argument("--fund", choices=["quick", "none"], default="quick")
    evidence = subparsers.add_parser("evidence")
    evidence.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "address":
            public_key = raw_public_key(load_signing_key(args.identity))
            print(f"did: {did_for_public_key(public_key)}")
            print(f"address: {args.address or ss58_address(public_key)}")
            return 0
        ctx = open_context(args.identity, resolve_network(args), args.state_dir, args.address, args.accept_new_genesis)
        if args.command == "status":
            credential = load_credential(ctx)
            print(json.dumps({
                "did": ctx.did,
                "address": ctx.address,
                "network": ctx.network.name,
                "genesis": ctx.genesis,
                "last_fund": last_success(ctx, "fund"),
                "credential_model": credential.get("model") if credential else None,
                "credential_expires_at": credential.get("expires_at") if credential else None,
                "spends": sum(1 for entry in ctx.ledger if entry.get("kind") == "spend" and entry.get("ok")),
            }, indent=2))
        elif args.command == "fund":
            print(json.dumps(fund(ctx, args.tier, args.provider, args.force), indent=2))
        elif args.command == "enroll":
            credential = enroll(ctx)
            print(f"enrolled: model {credential['model']}, quota {credential['remaining_quota']} FLOP")
        elif args.command == "spend":
            backend = build_backend(args)
            task = load_task(args.task) if args.task else {"kind": "prompt", "path": None, "title": "adhoc", "prompt": args.prompt}
            entry = run_task(ctx, backend, task)
            print(json.dumps({key: entry.get(key) for key in ("backend", "settlement", "request_id", "cost_flop", "miner")}, indent=2))
        elif args.command == "run":
            print(json.dumps(run_cycle(ctx, build_backend(args), args.queue, args.max_tasks, args.budget_flop, args.fund), indent=2))
        else:
            evidence = export_evidence(ctx, args.output)
            print(f"public evidence: {args.output} ({evidence['spend_count']} spends)")
        return 0
    except (FileExistsError, OSError, parcel_module.ParcelError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
