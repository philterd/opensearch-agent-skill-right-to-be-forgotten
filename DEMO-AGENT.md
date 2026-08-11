# Demo Walkthrough: driving the skill by prompt

This is the skill used the way it is meant to be used: you describe the erasure
request in your own words and the agent runs the phases in `SKILL.md`. Nobody
types a `forget_me.py` command.

[`DEMO.md`](DEMO.md) is the same workflow at the command line, and is the better
reference when you want to know exactly what a phase does.

## Before you start

Install the skill into an Agent-Skills-compatible client (Claude Code, Cursor,
Kiro, Copilot, Windsurf, Gemini CLI, Codex), point it at this directory, and
seed a corpus:

```bash
uv run python scripts/forget_me.py setup
uv run python scripts/forget_me.py seed-courtlistener --limit 1200
```

Court opinions because opinions describe people: roughly 8 descriptive
references per document, against 0.1 for a corpus of email. That difference
decides whether the indirect pass can work at all, which is why the agent
measures it in prompt 1 before searching.

Agent replies vary in wording. What should not vary is the sequence of commands
it runs and the fact that it refuses to write to your cluster.

---

## 1. The hard half: a person described, not named

> We've had a right-to-be-forgotten request against our `case-law` index. The
> person is a farmer who worked a large tract of land in Nelson county between
> 1903 and 1905, under a cropping contract he later asked the court to reform.
> We don't have a name. What can you find?

The description names nobody, which is the case keyword tools cannot handle.

**Expect the agent to** check connectivity, run `assess` on the index and report
that it comes back `rich`, then run `discover` with a profile and keywords it
derives from your sentence. It should then judge each candidate itself against
the criteria in `SKILL.md`, scoring confidence and quoting the exact text that
identifies the person, and present the audit report rather than a raw JSON dump.

The top candidate should be the opinion beginning *"This action was commenced
for the reformation of a contract under which the plaintiff farmed a large
amount of land in Nelson county, during the years 1903, 1904 and 1905."*

Ask **"why that one and not the second?"** if you want the reasoning surfaced.
Judging the near-misses is the part that distinguishes this from a search box.

## 2. Preview before anything changes

> Show me exactly what would change. Nothing should be modified yet.

**Expect the agent to** run `plan`, list the affected documents with confidence
scores and the snippets it would replace, and state plainly that nothing has
been written. Ask it to re-run at `strict_precision` to watch the set narrow.

## 3. Generate the remediation

> Generate the script. Redact rather than delete.

**Expect the agent to** run `export-curl`, produce `forget-me.sh` with one
command per document targeted by exact index and id, and write a hash-chained
erasure certificate. It should tell you it has changed nothing and hand you the
script to review.

Then: **"prove later that we did this"** should get you `verify-chain`. Edit a
byte in a certificate and ask again to see it report the break.

## What the agent does that a script cannot

The command line runs the same phases, so the difference is not the commands.
It is that Phase 2, deciding whether a document identifies *this* person and
could not reasonably describe someone else, is judgment rather than retrieval.
The agent reads each candidate, weighs the markers, scores its confidence and
extracts the exact span to remove. `discover` narrows 940 documents to 20; the
agent decides which of the 20 are the person.

Measured on the synthetic corpus, where the answer key is known: precision 1.00,
recall 0.91, and zero false positives across all eight decoy categories,
including near-misses that differ by a single marker. See
[`EVALUATION.md`](EVALUATION.md) for what that does and does not establish.
