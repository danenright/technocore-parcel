# Claude compatibility assessment

External Claude Code produced this bounded response after receiving only the task prompt. The local adapter retained the worker identity and private Technocore parcel capability, then submitted the response as a DID-signed result.

Technocore 0.11.4 compatibility assessment for cross-vendor agent coordination (Parcel v1)

Verdict: compatible. Parcel requires 0.11.0 or newer and depends on service behaviors 0.11.4 provides: a raw JSONL room export with an X-Room-Generation header, if_absent compare-and-set on private notes, write-time verification of did:key Ed25519 messages, and a manifest exposing the version. Assessed from client code and docs, not a live run.

## Three protocol invariants

1. **Independent verifiability (raw export).** The coordinator re-verifies every Ed25519 signature from the original record bytes and hashes the whole export with SHA-256, never trusting a server verdict. Anyone with the same export can reproduce the hash and verdicts offline, with no vendor SDK, account, or operator trust.

2. **Pinned, self-certifying authorship (retained signatures).** Each record keeps `from`, `sig`, `nonce`, and `text`; the signed payload is `room|nonce|text`, so a valid signature proves key control, room binding, and unmodified `task_id` and `worker_did`. Every claim, progress, and result sender must equal its `worker_did` and own the claim note, and when `expected_worker_did` is set the verifier rejects a differing claim owner. The pin is published in the public evidence for third-party checking.

3. **Generation-bound evidence.** Creation stores `X-Room-Generation` after posting the task event. Verification fetches the export, reads the claim note, fetches again, and fails if the stored generation is missing, differs, or changed between fetches. Evidence is anchored to one room lifetime, so records replayed into a recreated room cannot validate against the old parcel.

## Residual failure mode

Retention outruns the workflow. Notes and private rooms are reclaimed after inactivity, and the raw export is the only verification input. If a worker claims late or works past the retention window, the claim note can expire or the room can be reclaimed before the result lands; a correct result then fails with a missing claim or generation mismatch, indistinguishable from a hostile outcome. After reclaim nobody can re-fetch the export to re-derive `room_export_sha256`, so the public record becomes unreproducible.

**Mitigation:** Set a task deadline well inside the retention window and have workers post periodic progress events as keepalives; verify and export as soon as the result appears; store the raw export bytes and claim note beside the parcel (mode `600`, outside Git) so hash and signature verdicts stay reproducible; and treat the Git commit plus offline attestation, not the live room, as the durable record.
