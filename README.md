# LedgerMolty

An agent that keeps the public record for a small fleet of infrastructure agents, and posts about what that record shows — to Moltbook, the social network for AI agents.

It does one unusual thing: it talks about real work using numbers a stranger can independently verify. Most agents on that platform post philosophy. This one posts *"712 runs across two agents this week, 19 failures. cert-sentinel is the weaker of the two at 95 percent."*

Including the failures. A ledger that only reports good news isn't a ledger.

## The design problem

Moltbook is untrusted territory. Its own documentation warns agents never to send their API key anywhere else, and security researchers have documented indirect prompt injection as the platform's core risk. On top of that, the terms of service state that the operator is solely responsible for the agent's actions — *regardless of the degree of control or oversight exercised, and irrespective of whether the action was intended, authorised, foreseeable or known.*

So a hijacked agent isn't an embarrassment. It's a liability event.

The usual answer is "write a careful system prompt." That's not an answer — prompts can be argued with. This repo's answer is architectural: **the untrusted text and the ability to publish never exist in the same context.**

```
    reply on Moltbook  ──►  reader model  ──►  {topic, hostile, worth_answering}
        (untrusted)          (sees text)                    │
                                                            │  ◄── airgap
                                                            ▼
    fleet trust records ───────────────────────►  writer model  (never sees the text)
      (Query API)                                           │
                                                            ▼
                                                      rule filter
                                                            │
                                                            ▼
                                            reply — published only if it clears
                                            the guard and reads back as live
```

The reader can only emit three enum fields. An attacker who fully controls the input and fully succeeds gets to make one topic label wrong. `parse_label` rejects anything else — prose, commands, URLs, arrays — and returns an inert label that is never worth answering.

The writer receives that label plus facts pulled from your own API. `compose.assert_no_untrusted()` fails loudly if anyone ever adds a "just a bit of context" parameter, because airgaps decay silently.

Then `guard.py` runs last. It contains no model: link allowlist, length caps, promotional-phrase blocklist, credential-shape detection, instruction-shape detection. It cannot be talked out of anything.

## Where the material comes from

`GET /api/v1/query/agents/{slug}` on AiOps Enabler, once per fleet agent, using a free consumer key. `material.py` is deliberately **schema-tolerant**: each fact declares a list of candidate field names and searches the response recursively, so a field nested under `metrics` or named `successRate` still maps. Run this first:

```bash
python agent.py --describe-api
```

It prints every field the API actually returned, which of them mapped, and the exact facts the writer would receive. If something didn't map, that output tells you the real key name and it's a one-line fix in `FIELD_CANDIDATES`.

Only facts that survive `build_facts` can ever appear in a post. Everything else the API returns is invisible to the writer — which is the point: it can't casually mention a field nobody vetted. Credential-shaped keys (`api_key`, `secret`, `token`, `owner_email`) are stripped even from the diagnostics.

## Two lanes

**Posting is autonomous.** It reads the fleet's records, writes a post, publishes. It touches no foreign text at all, so there is nothing to inject. This is where the value is.

**Replies run in `auto`.** They are classified, drafted, and published — but only if the draft clears `guard.py` and then reads back as actually live (a comment that lands `pending` or gets spam-flagged fails the run rather than reporting as sent). The old advice was "stay on draft for the first month"; that is stale, because in CI `draft` mode writes to a file on a throwaway runner nobody ever reads. So the real choice is `off` or `auto`, and `auto` is safe: the writer never sees the untrusted text (the airgap), and nothing reaches the platform without passing the guard and the read-back assertion.

## Quick start

```bash
pip install -r requirements.txt
python tests/test_resident.py          # 85 tests, no framework needed
```

Fill in the five `<-- FILL IN` lines in `config.yml`, then:

```bash
export MOLTBOOK_API_KEY=moltbook_...
export AIOPS_QUERY_KEY=aiops_query_...
export ANTHROPIC_API_KEY=sk-ant-...

python agent.py --describe-api              # confirm the field mapping FIRST
python agent.py --setup-profile --dry-run   # see the disclosure line
python agent.py --once --dry-run            # compose everything, publish nothing
python agent.py --once                      # go live
python agent.py --once --replies            # also draft replies
```

`--dry-run` runs the entire pipeline including the guard, and publishes nothing. Use it until the output reads the way you want.

## Registering first

The agent must exist on Moltbook before this code can use it. Do this by hand, in your own terminal — not by telling an agent to "read the skill and follow the instructions," which is the exact pattern this repo exists to avoid.

```bash
curl -X POST https://www.moltbook.com/api/v1/agents/register \
  -H "Content-Type: application/json" \
  -d '{"name": "LedgerMolty", "description": "temporary"}'
```

Save the `api_key` immediately — it is shown once. Open the `claim_url`, verify your email, and post the verification tweet from your X account. Then `python agent.py --setup-profile` writes the real description.

## The verification puzzle

Moltbook hides new content until the agent solves an obfuscated arithmetic problem, and **ten consecutive failures suspend the account permanently.**

`solve_locally` handles it without a model call: it strips the injected symbols, recovers the words (including doubled-letter forms like `tWeNn-Tyy`), and computes the result. A model call is the fallback.

The important part is the stop condition. This agent halts at **two** consecutive failures and reports a failure, rather than spending the remaining eight attempts of your account's life. The count persists in the ledger, so a restart doesn't reset it.

## What it will not do

- Post more than once per period. The ledger enforces it; a retried workflow can't produce a duplicate, which the platform treats as spam.
- Publish any URL other than the one in `record_url`.
- Say "sign up", "check out", "try it free", or twenty other phrases. The platform's terms prohibit advertising content, so promotional phrasing is a terms breach, not a style problem.
- Emit anything shaped like a credential.
- Mention the product in a post. The disclosure lives in the profile description, where the platform already displays your X identity next to it. That is the whole promotional surface, and it is enough — anyone curious clicks through.

## Configuration

| Key | Meaning |
| --- | --- |
| `record_url` | The only URL that may ever be published. Blank is fine — the profile line then omits the link rather than shipping a broken one. |
| `enabler.fleet` | The published agent slugs LedgerMolty keeps the record for. Drafts are invisible to the Query API. |
| `submolt` | Which community to post in. |
| `posting.period` | `day` or `week` — one post per period. |
| `replies.mode` | `off`, `draft`, or `auto` (recommended — see above; `draft` only writes to an unread CI file). |
| `replies.max_per_run` | Cap on comments handled per run. |

Secrets are environment-only: `MOLTBOOK_API_KEY`, `AIOPS_QUERY_KEY`, `ANTHROPIC_API_KEY`. Nothing sensitive is in the config, so this repo is safe to keep public — which it should be. For an agent whose entire persona is transparency, open code isn't generosity, it's the costume.

## Blast radius

If this agent is completely compromised, the attacker gets: the ability to post as it, an LLM key, and a free read-only consumer key for public trust data. That is all. It holds no platform admin access, no cloud credentials, and has no path to production. `_request` refuses to build a URL for any host other than `www.moltbook.com`, so the Moltbook key cannot be exfiltrated by redirecting a request.

Keep it that way. Any future feature that needs a broader credential belongs in a different repo.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Ran and did its work — whether or not it had something to post. |
| `1` | Could not do its work: API failure, config error, crash, or the suspension guard tripping. |

Same `outcome`/`summary`/`metrics` envelope as the other agents in this fleet, so one platform integration covers all of them.

## Licence

MIT — see [LICENSE](LICENSE).
