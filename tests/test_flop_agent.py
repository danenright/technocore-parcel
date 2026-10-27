from __future__ import annotations

import importlib.util
import json
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("flop_agent", ROOT / "flop_agent.py")
assert SPEC and SPEC.loader
agent = importlib.util.module_from_spec(SPEC)
sys.modules["flop_agent"] = agent
SPEC.loader.exec_module(agent)

GENESIS = "0x99c172c095340ace980631f9123544c6bebd4839f3437e463533422d8be2d631"
ALICE_PUBLIC_KEY = bytes.fromhex("d43593c715fdd31c61141abd04a99fd6822c8558854ccde39a5684e7a56da27d")
ALICE = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
OTHER_MINER = "5DCYZ8eLJMHU5LfNs7v1vEh1Y1yWRFejMjxhGXUDFF49YwHv"
OWN_MINER = "5ED68Tgj9G6xtPnWKxzBkr7cij6nPR3kVXUYQ2RT27igsrH2"
DEVNET = agent.NETWORKS["devnet"]


def challenge_message(address: str, genesis: str = GENESIS, action: str = "flop-devnet-enroll/v1") -> str:
    return "\n".join(
        [
            "api.flop.network wants you to enroll a FLOP devnet gateway key with your account:",
            address,
            "",
            f"Action: {action}",
            f"Genesis: {genesis}",
            "Nonce: " + "c3" * 32,
            "Issued At: 2026-10-26T22:58:05.254Z",
            "Expiration Time: 2026-10-26T23:03:05.254Z",
        ]
    )


class FakeGateway:
    """Routes http_json calls to canned faucet and gateway responses and records them."""

    def __init__(self, ctx_address: str):
        self.address = ctx_address
        self.calls: list[tuple[str, str, dict | None, str | None]] = []
        self.fund_status = 200
        self.chat_status = 200
        self.completion = {
            "id": "gen-1",
            "model": "qwen/qwen2.5-0.5b-instruct",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "three risks"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13, "cost_flop": 0.25, "gflops_used": 1.5},
        }

    def __call__(self, method, url, payload=None, bearer=None, timeout=60):
        self.calls.append((method, url, payload, bearer))
        if url.endswith("/chain"):
            return 200, {"network": "devnet", "genesisHash": GENESIS}
        if url.endswith("/fund"):
            if self.fund_status != 200:
                return self.fund_status, {"error": "cooldown"}
            return 200, {"success": True, "amount": 25, "txHash": "0x" + "ab" * 32, "tier": "quick"}
        if "/enroll/wallet/challenge" in url:
            return 200, {
                "message": challenge_message(self.address),
                "nonce": "c3" * 32,
                "expires_at": int(time.time() * 1000) + 300_000,
            }
        if url.endswith("/enroll/wallet"):
            return 201, {
                "key": "flop_secret_bearer_value",
                "model": "qwen/qwen2.5-0.5b-instruct",
                "remaining_quota": 25,
                "expires_at": int(time.time() * 1000) + 86_400_000,
            }
        if url.endswith("/models/estimate"):
            return 200, {"estimated_cost_flop": round(payload["max_tokens"] * 0.006, 3)}
        if url.endswith("/chat/completions"):
            if self.chat_status != 200:
                return self.chat_status, {"error": "max_cost_exceeded"}
            return 200, self.completion
        if url.endswith("/keys/me"):
            return 200, {"credential": "owner", "remaining_quota": 24.75, "expires_at": None}
        raise AssertionError(f"unexpected request {method} {url}")


class AgentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.identity = self.root / "agent.env"
        self.seed = "11" * 32
        self.identity.write_text(f"SIGN_SEED={self.seed}\n", encoding="utf-8")
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self.seed))
        self.address = agent.ss58_address(agent.raw_public_key(key))
        self.gateway = FakeGateway(self.address)
        patcher = mock.patch.object(agent, "http_json", self.gateway)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = mock.patch.object(agent, "FUND_SETTLE_SECONDS", 0)
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.addCleanup(self.directory.cleanup)

    def context(self, address: str | None = None, accept_new_genesis: bool = False):
        return agent.open_context(self.identity, DEVNET, self.root / "state", address, accept_new_genesis)


class AddressTests(unittest.TestCase):
    def test_ss58_matches_substrate_reference_vector(self) -> None:
        self.assertEqual(agent.ss58_address(ALICE_PUBLIC_KEY), ALICE)
        self.assertEqual(agent.ss58_public_key(ALICE), ALICE_PUBLIC_KEY)

    def test_ss58_checksum_is_enforced(self) -> None:
        tampered = ALICE[:-1] + ("Z" if ALICE[-1] != "Z" else "Y")
        with self.assertRaises(agent.AgentError):
            agent.ss58_public_key(tampered)

    def test_did_and_account_share_one_ed25519_key(self) -> None:
        key = Ed25519PrivateKey.generate()
        public_key = agent.raw_public_key(key)
        did = agent.did_for_public_key(public_key)
        self.assertRegex(did, agent.parcel_module.DID_RE)
        decoded = agent.parcel_module.base58_decode(did.removeprefix("did:key:z"))
        self.assertEqual(decoded[2:], agent.ss58_public_key(agent.ss58_address(public_key)))

    def test_identity_file_format_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            identity = Path(directory) / "agent.env"
            identity.write_text("SIGN_SEED=not-hex\n", encoding="utf-8")
            with self.assertRaisesRegex(agent.AgentError, "64-hex"):
                agent.load_signing_key(identity)


class ChallengeTests(unittest.TestCase):
    def verify(self, message: str, address: str = ALICE, expires_at: int | None = None) -> None:
        agent.verify_challenge(
            message,
            address,
            GENESIS,
            DEVNET.gateway_url,
            expires_at if expires_at is not None else int(time.time() * 1000) + 60_000,
        )

    def test_live_statement_format_is_accepted(self) -> None:
        self.verify(challenge_message(ALICE))

    def test_purpose_line_of_v2_statement_is_accepted(self) -> None:
        lines = challenge_message(ALICE, action="flop-devnet-enroll/v2").split("\n")
        lines.insert(4, "Purpose: hermes-tools-v1")
        self.verify("\n".join(lines))

    def test_statement_for_another_account_is_refused(self) -> None:
        with self.assertRaisesRegex(agent.AgentError, "account"):
            self.verify(challenge_message(OTHER_MINER))

    def test_statement_for_another_chain_is_refused(self) -> None:
        with self.assertRaisesRegex(agent.AgentError, "genesis"):
            self.verify(challenge_message(ALICE, genesis="0x" + "00" * 32))

    def test_unexpected_action_or_extra_line_is_refused(self) -> None:
        with self.assertRaisesRegex(agent.AgentError, "action"):
            self.verify(challenge_message(ALICE, action="transfer"))
        with self.assertRaises(agent.AgentError):
            self.verify(challenge_message(ALICE) + "\nAmount: 1000")

    def test_expired_statement_is_refused(self) -> None:
        with self.assertRaisesRegex(agent.AgentError, "expired"):
            self.verify(challenge_message(ALICE), expires_at=1)


class FundAndEnrollTests(AgentTestCase):
    def test_identity_is_pinned_to_one_account(self) -> None:
        self.context()
        with self.assertRaisesRegex(agent.AgentError, "one DID and one account"):
            self.context(address=ALICE)

    def test_genesis_change_needs_explicit_acceptance(self) -> None:
        self.context()
        pin = self.root / "state" / "devnet" / "identity.json"
        value = json.loads(pin.read_text())
        value["genesis"] = "0x" + "00" * 32
        pin.write_text(json.dumps(value))
        with self.assertRaisesRegex(agent.AgentError, "genesis changed"):
            self.context()
        self.context(accept_new_genesis=True)

    def test_fund_respects_daily_address_cooldown(self) -> None:
        ctx = self.context()
        first = agent.fund(ctx)
        second = agent.fund(ctx)
        self.assertTrue(first["ok"])
        self.assertEqual(second["skipped"], "cooldown")
        self.assertEqual(sum(1 for call in self.gateway.calls if call[1].endswith("/fund")), 1)
        self.assertTrue(agent.fund(ctx, force=True)["ok"])

    def test_linked_grant_requires_a_human_verification_provider(self) -> None:
        with self.assertRaisesRegex(agent.AgentError, "provider"):
            agent.fund(self.context(), tier="linked")

    def test_enrollment_signs_exact_challenge_and_keeps_bearer_private(self) -> None:
        ctx = self.context()
        credential = agent.enroll(ctx)
        post = next(call for call in self.gateway.calls if call[1].endswith("/enroll/wallet"))
        payload = post[2]
        ctx.key.public_key().verify(bytes.fromhex(payload["signature"][2:]), payload["message"].encode())
        self.assertEqual(payload["address"], self.address)
        self.assertEqual(credential["key"], "flop_secret_bearer_value")
        self.assertEqual(stat.S_IMODE(ctx.credential_path.stat().st_mode), 0o600)
        self.assertNotIn("flop_secret_bearer_value", ctx.ledger_path.read_text())
        self.assertEqual(stat.S_IMODE(ctx.ledger_path.stat().st_mode), 0o600)

    def test_ledger_refuses_to_record_the_bearer(self) -> None:
        ctx = self.context()
        agent.enroll(ctx)
        with self.assertRaisesRegex(agent.AgentError, "credential"):
            agent.append_ledger(ctx, {"kind": "spend", "error": "echo flop_secret_bearer_value"})


class SpendTests(AgentTestCase):
    def backend(self, excluded: set[str] | None = None):
        return agent.GatewayBackend(512, 4.0, excluded or set())

    def test_gateway_spend_records_hashes_not_text(self) -> None:
        ctx = self.context()
        task = {"kind": "prompt", "path": None, "title": "Review", "prompt": "Find three risks"}
        entry = agent.run_task(ctx, self.backend(), task)
        self.assertEqual(entry["cost_flop"], 0.25)
        self.assertEqual(entry["settlement"], "brokered")
        ledger = ctx.ledger_path.read_text()
        self.assertNotIn("Find three risks", ledger)
        self.assertNotIn("three risks", ledger)
        bearer_calls = [call for call in self.gateway.calls if call[1].endswith("/chat/completions")]
        self.assertEqual(bearer_calls[0][3], "flop_secret_bearer_value")
        self.assertEqual(bearer_calls[0][2]["max_cost_flop"], 4.0)
        self.assertEqual(entry["estimated_cost_flop"], 3.072)
        results = list(ctx.results_dir.iterdir())
        self.assertEqual(stat.S_IMODE(results[0].stat().st_mode), 0o600)

    def test_metered_cost_is_read_from_the_gateway_receipt(self) -> None:
        del self.gateway.completion["usage"]["cost_flop"]
        self.gateway.completion["x_flop_receipt"] = {
            "aggregateGn": "390",
            "channelId": "0x" + "dc" * 32,
            "miner": OTHER_MINER,
            "priceSnapshot": {"flopPerGn": "0.003"},
            "reservedRaw": "3168000000000000149",
            "settlementStatus": "pending",
        }
        task = {"kind": "prompt", "path": None, "title": "t", "prompt": "p"}
        entry = agent.run_task(self.context(), self.backend(), task)
        self.assertEqual(entry["cost_flop"], 1.17)
        self.assertEqual(entry["aggregate_gn"], 390)
        self.assertAlmostEqual(entry["reserved_flop"], 3.168)
        self.assertEqual(entry["channel"], "0x" + "dc" * 32)
        self.assertEqual(entry["settlement_status"], "pending")

    def test_receipt_without_price_leaves_cost_unknown(self) -> None:
        self.assertIsNone(agent.receipt_cost({"aggregateGn": "390"})["cost_flop"])
        self.assertIsNone(agent.receipt_cost(None)["reserved_flop"])

    def test_cost_cap_below_worst_case_is_refused_before_spending(self) -> None:
        task = {"kind": "prompt", "path": None, "title": "t", "prompt": "p"}
        with self.assertRaisesRegex(agent.AgentError, "worst case for --max-tokens 512 is 3.072 FLOP"):
            agent.run_task(self.context(), agent.GatewayBackend(512, 1.0, set()), task)
        self.assertFalse(any(call[1].endswith("/chat/completions") for call in self.gateway.calls))

    def test_gateway_cost_refusal_explains_the_fix(self) -> None:
        self.gateway.chat_status = 402
        task = {"kind": "prompt", "path": None, "title": "t", "prompt": "p"}
        with self.assertRaisesRegex(agent.AgentError, "raise --max-cost-flop or lower --max-tokens"):
            agent.run_task(self.context(), self.backend(), task)

    def test_default_cap_depends_on_backend(self) -> None:
        args = agent.build_parser().parse_args(["spend", "--prompt", "p"])
        self.assertEqual(agent.build_backend(args).max_cost_flop, agent.GATEWAY_DEFAULT_MAX_COST_FLOP)
        args = agent.build_parser().parse_args(
            ["spend", "--prompt", "p", "--backend", "cli", "--flop-cli", "flop", "--miner", OTHER_MINER, "--model", "m"]
        )
        self.assertEqual(agent.build_backend(args).max_cost_flop, agent.CLI_DEFAULT_MAX_COST_FLOP)

    def test_gateway_spend_to_excluded_miner_is_refused(self) -> None:
        self.gateway.completion["x_flop_receipt"] = {"miner": OWN_MINER}
        task = {"kind": "prompt", "path": None, "title": "t", "prompt": "p"}
        with self.assertRaisesRegex(agent.AgentError, "excluded miner"):
            agent.run_task(self.context(), self.backend({OWN_MINER}), task)

    def test_cycle_funds_spends_and_settles_queue_within_budget(self) -> None:
        ctx = self.context()
        queue = self.root / "queue"
        queue.mkdir()
        for index in range(3):
            (queue / f"{index}.json").write_text(json.dumps({"title": f"t{index}", "prompt": f"p{index}"}))
        (queue / "bad.json").write_text("{}")
        self.gateway.completion["usage"]["cost_flop"] = 1.0
        summary = agent.run_cycle(ctx, self.backend(), queue, max_tasks=5, budget_flop=5.5, fund_tier="quick")
        self.assertTrue(summary["funded"]["ok"])
        self.assertEqual(summary["spent"], 2)
        self.assertEqual(sorted(path.name for path in (queue / "done").iterdir()), ["0.json", "1.json"])
        self.assertTrue((queue / "2.json").exists())

    def test_cycle_without_funding_skips_faucet(self) -> None:
        queue = self.root / "queue"
        queue.mkdir()
        agent.run_cycle(self.context(), self.backend(), queue, 1, 4.0, "none")
        self.assertFalse(any(call[1].endswith("/fund") for call in self.gateway.calls))

    def test_parcel_task_claims_and_posts_signed_result(self) -> None:
        ctx = self.context()
        private = {
            "parcel_version": 1,
            "task_id": "a" * 16,
            "service": "https://chat.technocore-lab.com",
            "room": "p-parcel-" + "b" * 20,
            "namespace": "p-parcel-" + "b" * 20,
            "task": {"title": "Review", "instructions": "Return improvements"},
            "coordinator_did": "did:key:z6MkrNkU2iHvF1YAM7JQxgzU8a8YgB6QGCCKBFzQbRmpZ1GM",
            "events": [],
        }
        path = self.root / "parcel.json"
        agent.parcel_module.write_private_json(path, private, replace=False)
        self.gateway.completion["choices"][0]["message"]["content"] = "x" * 5000
        with (
            mock.patch.object(agent.parcel_module, "claim_parcel") as claim,
            mock.patch.object(
                agent.parcel_module, "worker_event", return_value={"verified_record": {"seq": 9}}
            ) as result,
        ):
            entry = agent.run_task(ctx, self.backend(), agent.load_task(path))
        claim.assert_called_once()
        body = result.call_args.args[3]
        self.assertLessEqual(len(body), agent.parcel_module.MAX_EVENT_BODY_CHARS)
        self.assertTrue(entry["reply_truncated"])
        self.assertEqual(entry["parcel_result_seq"], 9)
        self.assertNotIn(private["room"], ctx.ledger_path.read_text())


class CliBackendTests(AgentTestCase):
    def fake_cli(self, result: dict) -> str:
        script = self.root / "fake_flop.py"
        script.write_text(
            textwrap.dedent(
                f"""
                import json, sys
                args = sys.argv[1:]
                assert args[args.index("--miner") + 1] == {OTHER_MINER!r}
                print("opening channel")
                print(json.dumps({result!r}))
                """
            )
        )
        return f"{sys.executable} {script}"

    def backend(self, command: str, excluded: set[str] | None = None):
        return agent.CliBackend(
            command,
            OTHER_MINER,
            "qwen/qwen2.5-0.5b-instruct",
            0.01,
            self.root / "agent.json",
            "FLOP_KEY_PASSWORD",
            "auto",
            None,
            excluded or set(),
        )

    def test_direct_session_result_is_parsed(self) -> None:
        command = self.fake_cli(
            {"reply": "hello", "miner": OTHER_MINER, "channel": 42, "settlementTx": "0xabc", "verifiedTurns": 1}
        )
        spent = self.backend(command).complete(self.context(), "Say hello.")
        self.assertEqual(spent["channel"], 42)
        self.assertEqual(spent["settlement_tx"], "0xabc")
        self.assertEqual(spent["settlement"], "direct")

    def test_own_miner_is_refused_before_running(self) -> None:
        with self.assertRaisesRegex(agent.AgentError, "excluded miner"):
            self.backend("flop", {OTHER_MINER})

    def test_unrecognised_cli_output_fails_closed(self) -> None:
        command = self.fake_cli({"status": "ok"})
        with self.assertRaisesRegex(agent.AgentError, "unrecognised"):
            self.backend(command).complete(self.context(), "Say hello.")


class EvidenceTests(AgentTestCase):
    def test_evidence_has_no_bearer_prompt_or_reply(self) -> None:
        ctx = self.context()
        agent.fund(ctx)
        task = {"kind": "prompt", "path": None, "title": "Private title", "prompt": "Private prompt"}
        agent.run_task(ctx, agent.GatewayBackend(512, 4.0, set()), task)
        output = self.root / "evidence.json"
        evidence = agent.export_evidence(ctx, output)
        serialized = output.read_text()
        for secret in ("flop_secret_bearer_value", "Private prompt", "Private title", "three risks"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(evidence["spend_count"], 1)
        self.assertEqual(evidence["fund_count"], 1)
        self.assertEqual(evidence["spent_flop"], 0.25)


class NetworkResolutionTests(unittest.TestCase):
    def args(self, **values):
        defaults = {"network": "devnet", "faucet_url": None, "gateway_url": None, "expected_genesis": None}
        defaults.update(values)
        return mock.Mock(**defaults)

    def test_unpublished_network_needs_explicit_endpoints(self) -> None:
        with self.assertRaisesRegex(agent.AgentError, "no published endpoints"):
            agent.resolve_network(self.args(network="testnet"))

    def test_explicit_testnet_endpoints_are_accepted(self) -> None:
        network = agent.resolve_network(
            self.args(
                network="testnet",
                faucet_url="https://faucet.example/",
                gateway_url="https://gateway.example/api/v1",
                expected_genesis=GENESIS,
            )
        )
        self.assertEqual(network.faucet_url, "https://faucet.example")
        self.assertEqual(network.expected_genesis, GENESIS)


if __name__ == "__main__":
    unittest.main()
