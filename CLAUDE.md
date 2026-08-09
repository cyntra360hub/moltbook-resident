# CLAUDE.md — read this before changing anything

LedgerMolty is a public agent on Moltbook (`u/ledgermolty`). It posts the
verified operating record of a small fleet of infrastructure agents, and
answers when asked. It is operated by AiOps Enabler, a platform whose entire
product is *proof rather than claims* — so an agent that publishes a wrong
number, or reads as an advert, damages the thing it exists to demonstrate.

Everything below was learned the hard way. Please don't undo it.

## The five hard rules

**1. The airgap. This is the one that matters most.**

Untrusted text (anyone's comment, anyone's post) goes into `triage.py` and
NOWHERE else. `compose.py` never sees it — it receives only a three-field
`Label` plus facts from our own API.

Prompt injection works by getting attacker text into a context that can then
act. Here the only available "action" is filling three enum fields, so an
attacker with total control of the input gets to make a topic label wrong.
That is the whole blast radius, and it is a property of the *structure*, not
of the prompt.

`compose.assert_no_untrusted()` raises if anyone passes a field named
`message`, `comment`, `text`, `body`, `content`, or `untrusted_text` to the
writer. If you find yourself wanting to widen that interface "just a little,
for context" — that is precisely the erosion it exists to stop. Don't.

**2. `guard.py` contains no model and must never contain one.**

It is the last thing that runs before anything is published: link allowlist,
promotional-phrase blocklist, credential-shape detection, instruction-shape
detection, numeric fact-checking, title novelty. Plain `if` statements cannot
be argued with. Everything else here can.

**3. Every figure must trace to a fact.**

`guard.check_numbers()` extracts every number from a draft and requires it to
appear in the facts given. A model asked to write about 14 instrumented agents
will occasionally write 15 — harmless anywhere else, fatal for an agent whose
claim is that its numbers are checkable. A numeric slip triggers a recompose;
a policy breach does not (that is a prompt problem, and rerolling won't fix it).

**4. No promotion, ever.**

Moltbook's terms prohibit advertising and marketing content, and the platform
bans repetitive posting. The ONLY promotional surface is the profile
description, which carries one link. Posts never contain a URL and never name
the operator — the profile already says it.

`guard.check_title_novelty()` blocks a headline too close to a recent one.
This exists because the underlying data barely moves, so the model converges
on one headline and reuses it forever. Three near-identical posts went out
before this was added.

**5. Detection is not failure — but invisible success is.**

A run that finds nothing to post is a success. A run that *publishes something
nobody can see* is a failure, and must report as one. Three separate bugs here
all had the same shape: the agent believed it had succeeded when it hadn't
(a 404 after the work was done; duplicate posts from split state; a post that
failed its verification puzzle and was hidden). If you add a code path where
"the API accepted it" and "it actually worked" can differ, make the run fail.

## Facts about the two APIs, learned by trial

**AiOps Enabler**
- Query API lives at `https://api.aiopsenabler.com` — NOT `aiopsenabler.com`,
  where the single-page frontend answers 200 with HTML for unknown paths.
  `material.py` detects that and says so, because a bare `JSONDecodeError`
  sends people looking at their key.
- Auth: `Authorization: Bearer <consumer key>`, from `AIOPS_QUERY_KEY`.
- `GET /api/v1/query/agents/{slug}` returns NO metrics — its
  `field_provenance` map claims `metrics: platform_verified`, but no metrics
  object is present. Known platform bug.
- `GET /api/v1/leaderboard` DOES carry `tasks_handled` and `success_rate` for
  every agent, in one call. That is why the fleet reader uses it.
- `success_rate` arrives as a 0–1 fraction, not a percentage. `as_rate()`
  normalizes it.
- Field names are not pinned: `FIELD_CANDIDATES` lists alternatives and
  searches the response recursively. `--describe-api` prints what actually
  came back. Prefer adding a candidate over hardcoding a name.

**Moltbook**
- The API key goes to `www.moltbook.com` and nowhere else. `_request` refuses
  to build a URL for any other host.
- `GET /api/v1/agents/me/posts` works. `/agents/{name}/posts` 404s.
- **Unverified content is invisible** — excluded from feeds, search, and
  `/agents/me/posts`. It appears only in the profile's "Best of" sidebar. So a
  post that fails its puzzle is unreachable AND invisible to our own duplicate
  check. We withdraw it and fail the run.
- Posting returns an arithmetic challenge, obfuscated with injected symbols
  and random capitalisation. `solve_locally()` handles simple two-number
  problems; the model handles the rest. **Ten consecutive failures suspend the
  account permanently — we stop at two.** Never raise that limit.
- `/home`'s `activity_on_your_posts` mixes comments on OUR posts with posts by
  others that MENTION us. They need opposite handling: reply to comments on our
  own threads; reply to the POST when someone mentions us; never touch other
  people's comments in a thread we don't own. Barging into strangers'
  conversations is the one behaviour this agent must not have.

## Things that will look like bugs but aren't

- `already posted for <date>` — the ledger working. One post per period.
- `mapped facts: NONE` in `--describe-api` — field names changed; add a
  candidate to `FIELD_CANDIDATES`.
- A cache warning on the first workflow run — no prior ledger to restore.
- The workflow file is named `report-to-aiops-enabler.yml` but contains the
  whole pipeline. **The filename is load-bearing**: the AiOps Enabler binding
  keys on repo + workflow filename, and renaming it silently breaks reporting
  (404 NOT_BOUND, discovered only in the Actions log).

## Before you commit

```bash
python tests/test_resident.py        # must be green, no exceptions
python agent.py --describe-api       # confirms the fleet data still maps
python agent.py --once --dry-run     # composes everything, publishes nothing
```

`--dry-run` runs the entire pipeline including every guard. Use it liberally;
it costs a few cents and nothing reaches the platform.

New rules need a fixture case and two tests: one that it fires, one that it
does NOT fire on the benign version of the same pattern. The negative test is
the one that matters — a rule with no negative test is a future false positive,
and a scanner that cries wolf gets muted.

## Secrets

Environment only, never in a file: `MOLTBOOK_API_KEY`, `AIOPS_QUERY_KEY`,
`ANTHROPIC_API_KEY`. The repo is public. `state/` is gitignored because it
holds drafts derived from other agents' comments.

Blast radius, deliberately small: full compromise of this agent yields the
ability to post as it, an LLM key, and a free read-only key for public trust
data. No cloud credentials, no platform admin, no path to production. Keep it
that way — any feature needing a broader credential belongs in another repo.
