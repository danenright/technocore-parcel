# Opt-in recovery for interrupted workers

Create with `--lease-seconds 300` to use parcel version 2. Version 1 remains the
default, with its original permanent claim. Old clients reject version 2; do not
manually convert an existing parcel. Each worker must use its own copy of the parcel
file and serialize operations on that copy.

For a leased parcel:

- `claim` prints a `claim-id` identifying this attempt. Keep it with the work.
- `renew PARCEL --claim-id ID` extends an unexpired claim by the configured duration.
  Renewal is explicit; progress does not renew implicitly. Renew before the deadline.
- `progress PARCEL --claim-id ID --message TEXT` commits a signed draft checkpoint.
- After expiry, another `claim` replaces the old claim using compare-and-set. It
  returns the last committed checkpoint after verifying its room signature and room
  generation. The replacement can continue from that text. A pinned worker DID still
  applies: omit that pin at creation if different workers should be eligible.
- `result PARCEL --claim-id ID --text TEXT` publishes a signed candidate, then atomically
  commits that exact result. A stale attempt cannot replace it. Retries of that exact
  result are allowed and count once; different results are refused.
- The Claude adapter's prepared prompt includes `claim_id` and `checkpoint`; submit
  with `--claim-id` from that prompt. This prevents an old response being relabeled
  with a newly prepared attempt on the same local file.

The claim note is the mutable coordination authority. Its CAS update selects the
winner; signed task, claim and result events establish attribution. Verification
accepts only the committed result for the current attempt, ignoring historical
results from superseded attempts. This is not an assessment of the result's quality.

## Boundaries

Deadlines use client UTC clocks, not server-enforced leases. Participants must
cooperate and keep clocks synchronized. A delayed operation can commit after its
local deadline check if nobody has changed the note; CAS fencing, not wall-clock
precision, prevents it from overwriting a replacement. Anyone who knows the namespace
can still edit its notes. This feature does not provide hostile-worker isolation,
exactly-once external side effects, settlement, or protection from rollback/tampering
by a capability holder. Never share the same local parcel file between concurrent
processes. Task and checkpoint content remain untrusted data, never shell commands.

If publication is interrupted, no completion is committed and the lease remains
recoverable after expiry. A posted candidate is not accepted until its CAS succeeds.
If the completion CAS succeeds but its response is lost, retrying the same result
is allowed even after expiry and counts once. If a claim note disappears or the
checkpoint's signed record is no longer retained, recovery fails closed and requires
coordinator intervention. A checkpoint is limited by the existing room-message and
8,192-character note limits; keep drafts small. The original room task announcement
must remain available for full verification. A successful `watch` is a snapshot,
not a durable guarantee about future note contents.

## Offline verification

Run from the repository root after installing `requirements.txt`:

```bash
python -m unittest discover -s tests -p test_recovery.py -v
```

The tests use fake identities, an injected clock and a fake CAS transport; no service
calls. They exercise an interrupted stanza writer, replacement and completion,
renewal, CAS races, stale same-DID attempts, lost responses, duplicate delivery and
missing checkpoint evidence. Existing tests separately verify Ed25519 signatures.
These checks do not establish live-service or distributed-clock behavior.
