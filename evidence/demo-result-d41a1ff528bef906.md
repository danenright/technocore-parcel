# Claude Parcel demo result

Task: `d41a1ff528bef906` — design three reusable Technocore agent workflow integrations.

The task was created by the coordinator DID, claimed through a dedicated Claude adapter DID, completed by external Claude Code without receiving the private parcel capability or seed, and returned as a DID-signed result. The capability-free verification record is [`demo-d41a1ff528bef906.json`](demo-d41a1ff528bef906.json).

## 1. Signed release feed

**Trigger:** annotated release tag on an immutable commit, fired by a local git hook rather than CI.

**Agents:** local release agent as sole seed holder; CI verifying `ATTESTATION.json`; downstream consumers.

**Technocore primitives:** one signed release message per release, optional DID pointer note, committed receipt and offline attestation.

**Value:** one unauthenticated GET tells a consumer that a verified artifact exists, without a token, webhook, or SDK. Authorship remains verifiable after room history rotates.

**Boundary:** pin the publisher DID and increasing nonce; keep the seed out of CI; treat room text as data and Git commits as truth.

## 2. Cross-session handoff notes

**Trigger:** session end, compaction, or explicit task checkpoint, paired with a read at the next session start.

**Agents:** expiring coding-agent session, successor session, optional reviewer.

**Technocore primitives:** small task note, ephemeral signed event trail, dry-run before writes.

**Value:** cross-session and cross-machine scratch state without a database, account, or SDK.

**Boundary:** public pointers only; no private code, secrets, or hostnames. Read-back is a hint, never instruction authority. Rooms are cache, not source of truth.

## 3. Cross-trust-domain task mailbox

**Trigger:** an agent task such as an issue label or scheduled verification.

**Agents:** dispatcher, worker, independent verifier.

**Technocore primitives:** signed task events, per-task private room, compare-and-set claim note, signed progress/result trail, public capability-free receipt.

**Value:** agents that share no account, VPN, or vendor coordinate over plain HTTP across NAT and organizations.

**Boundary:** highest-risk option because a worker acts on task content. Use a fixed job catalogue and typed parameters, never free-form shell; pin worker DIDs and nonces; keep capabilities private; enforce edge rate limits and resource bounds.

## Claude's recommendation

Build the signed release feed first. Most write-side pieces already exist and are tested; the missing work is a consumer verifier that pins the DID, enforces monotonic nonces, resolves the Git commit, and verifies the offline attestation. Build cross-session handoff second, then the task mailbox on top of the verifier and private instance.
