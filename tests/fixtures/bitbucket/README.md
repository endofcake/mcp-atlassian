# Golden Bitbucket Data Center fixtures

Synthetic response bodies backing the wire-shape guards for the Bitbucket read
surface:

- `bb9/`: the DC 9.x shape set.

The inline fixtures in the unit tests encode intent (what shape each model
must tolerate). These bodies pin full response envelopes (field presence and
nesting, pagination keys, link structures) so a shape drift fails a test
instead of surfacing at runtime.

## What the corpus covers

- **Repository surface**: branches (default and non-default, including the
  legacy `latestChangeset` alias), tags (empty page), both default-branch
  endpoint shapes, commit listings with inline author/committer objects,
  and browse results for a text file window, a directory, and a binary file.
- **Pull-request surface**: projects, repositories, PR get/list, diffs,
  commits, and an activity stream mixing comments (with tasks and threads),
  approvals, rescopes, and a merge.
- **Deliberate irregularities**, kept to exercise parse tolerance: a few
  inconsistent SHA lengths in the commit and browse bodies, Cloud-style
  pagination keys (`pagelen`, `page`) beside the DC envelope, epoch-second
  timestamps in some pull-request bodies beside the epoch-millisecond norm,
  a commit whose git author name does not match its user slug, and one
  pull-request commit (`pr_commits.json`, first value) whose abbreviated
  `displayId` does not abbreviate its own hash.

## Synthetic-value invariants

Every value in the corpus is synthetic, and
`tests/unit/bitbucket/test_golden_fixtures.py` enforces the floor
self-referentially:

- **Hosts and emails.** Every URL host and email domain is under the
  reserved `example.com` domain.
- **Git object IDs.** Every commit/blob hash (and abbreviated display ID)
  starts with the marker prefix `f1ce`, and every abbreviation is a prefix
  of its full hash (one enumerated exception under deliberate
  irregularities above).
- **Timestamps.** Every `authorTimestamp` / `committerTimestamp` /
  `createdDate` / `updatedDate` falls in calendar year 2021, in the unit
  (milliseconds or seconds) native to its body.
- **Activity, comment, and user IDs.** Activity IDs sit in `710001+`,
  comment IDs in `720001+` (both strictly ordered so pagination and
  threading logic see plausible sequences), and every user object's numeric
  ID in `730001+`.
- **Exempt low-value structural IDs.** Project, repository, and
  pull-request ordinals (and PR numbers quoted in commit-message text) stay
  as small integers (`1`–`16`). These are per-container sequence numbers
  that recur in virtually every Bitbucket instance and identify nothing on
  their own. Keeping them small keeps the bodies readable.

## Adding or regenerating fixtures

Compose new bodies to the same invariants. The hygiene test fails on any
value outside them. Two caveats:

- The golden tests pin fixture-specific values (default-branch name, cursor
  offsets, counts), so replacing a body means reconciling those assertions.
- Keep cross-references consistent: reuse the same synthetic hash wherever
  one object is referenced twice, and keep ID/timestamp ordering monotonic
  where the surface implies it.
