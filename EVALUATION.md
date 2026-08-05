# Evaluating indirect identification

How to build ground truth for the hard half of an erasure request, and what the
resulting numbers do and do not tell you.

> **Status:** this document describes a method. The harness is not implemented.
> The demo corpus in `scripts/seed_demo.py` carries hand-built labels, and
> `mail-enron` has none.

## Why this needs a document

Validating the direct pass is trivial. `discover-direct` finds documents
containing a literal identifier, and anyone can confirm a hit by searching the
text for the same string. The answer key writes itself.

The indirect pass has no such key. When a document says *"the sole senior
frontend engineer on Checkout who resigned in March"*, deciding whether that
identifies a particular person is not a property of the document. It depends on
how many people fit the description, which the document does not say and the
index does not contain. Producing a key by hand means an assessor reading every
document and making that call, which does not reproduce, does not scale, and on
a real corpus means performing the identification the tool exists to remediate.

The method below avoids assessors by taking labels from data the corpus already
holds.

## The core idea

Most real corpora have two channels:

- a **naming channel** that identifies people in structured form (an email
  header, a `user.id` field, a git author, a ticket assignee);
- a **describing channel** of unstructured text where people are talked about.

Discovery searches the describing channel. So the naming channel is available as
a label source without contaminating the search. Label from the names, evaluate
on the descriptions.

This is what makes the approach portable. The specific fields differ everywhere,
the structure does not.

## How it fits together

```mermaid
flowchart TD
    C["Corpus"]
    C --> N["Naming channel<br/>from / to / cc, user.id,<br/>git author, ticket assignee"]
    C --> D["Describing channel<br/>subject + body:<br/>the field discovery searches"]

    N --> ID["Identifier values<br/>and alias variants"]
    N --> R["Roster: who existed, when,<br/>with what attributes"]

    D --> M["Masked corpus"]
    ID -- "remove every variant" --> M

    ID --> L1["Labels 2a<br/>documents that contained<br/>an identifier<br/>(factual)"]
    R --> L2["Labels 2b<br/>anonymity set size k<br/>(model-dependent)"]

    M --> A{"Leakage audit:<br/>did any variant survive?"}
    ID --> A
    A -- "yes" --> STOP["Fail the run.<br/>The metric would be fiction."]
    A -- "no" --> DISC

    R --> Q["Profiles generated<br/>per subject"]
    Q --> DISC

    DISC["discover<br/>hybrid BM25 + neural"] --> EV["agent judgment<br/>+ filter_flagged"]
    EV --> SP["identifying_snippets<br/>redaction spans"]

    DISC --> S1["recall@k"]
    EV --> S2["precision, recall,<br/>FP per 1k documents"]
    SP --> S3["span validity,<br/>over-redaction ratio"]

    L1 --> S1
    L1 --> S2
    L2 --> S2
    L1 --> S3
```

Read it as two paths out of one corpus that reconverge only at the bottom. The
searchable text the pipeline sees is the masked describing channel and nothing
else. The naming channel produces the labels, the mask, and the roster, and it
reaches the pipeline at one point only: as the generated profile, which stands in
for the description a real erasure request would supply. That is an input, not an
answer. It says who to look for, never which documents are positive.

Everything else the naming channel produces stays on the label side until
scoring, which is what keeps the measurement honest.

The audit is a gate rather than a warning: if one alias survives masking, the
document is trivially retrievable and every number below it is meaningless.

## The method

### 1. Identify the naming channel

Find the fields that identify people and confirm they are outside the field
discovery searches (`--text-field`, default `message`). If the two overlap, the
evaluation is circular and nothing below is valid.

### 2. Derive labels

Two schemes, usable together.

**2a. Held-out identifier (mention recovery).** For a chosen subject, the
positives are every document whose text contains one of their identifiers. Mask
those identifiers, and evaluate on what remains. The label is a fact about the
corpus: the string was there before you removed it.

This measures whether residual context alone re-links a document to the person,
which is the capability the skill claims.

**2b. Anonymity set size (roster).** Given a population of people active in the
document's time window, and the markers a document asserts, count how many
population members satisfy all of them:

| Anonymity set size | Reading |
| :--- | :--- |
| 1 | identifies the subject |
| 2 to ~5 | singles out a small group, arguably still personal data |
| large | describes a role, not a person |

This is GDPR Recital 26's uniqueness test expressed as a count, and it produces
graded labels on a principled basis rather than an assessor's scale. It also
calibrates the `precision_mode` thresholds, which are otherwise chosen by feel.

Unlike 2a, this scheme is model-dependent, not factual. See
[Limitations](#limitations).

### 3. Audit the holdout, and fail loudly

If masking is incomplete the metric is fiction, because a single surviving
mention makes the document trivially retrievable. Treat this as a gate that
fails the run, not a warning. Check for:

- **name variants**: full name, surname alone, given name alone, initials, login
  or alias, every email address the person used;
- **quoted reply chains**, where a name masked in one document reappears inside
  another document's quoted block;
- **document ids**, which frequently embed a name. `seed_enron.py:189` yields
  `f"{custodian}/{folder}/{num}"`, so the custodian surname is in every id;
- **signature blocks**, where a direct phone number or extension identifies as
  well as a name does;
- **container names**: a folder, mailbox, or index named after the subject.

The last two are a judgment call. An unmasked phone number is arguably a true
positive for identification rather than leakage. Decide which effect you are
measuring and record the decision.

### 4. Construct queries mechanically

The profile is an input to the pipeline, and writing one by hand per subject
measures the prose as much as the system. Generate profiles from structured
attributes, or generate several wordings per subject and report the spread
across them. A large spread is itself a finding: it says the tool's output
depends heavily on how the request is phrased, which users need to know.

### 5. Run the pipeline

`discover` (and `discover-direct` where identifiers are supplied), then the
Phase 2 evaluation, then `plan`. Nothing is written to the cluster, so scoring
reads `plan` output rather than cluster state.

The evaluating agent must not see the labels. The demo corpus enforces this:
document ids are opaque digests rather than names like `sub-1`, and `seed-demo`
writes its answer key to `gdpr-eval/demo-ground-truth.json` instead of printing
it. Apply the same discipline to any corpus you build.

### 6. Score three stages separately

The pipeline has three stages that fail independently, and a single end-to-end
number hides which one broke.

| Stage | Output | Metric |
| :--- | :--- | :--- |
| `discover` | candidate list | recall@k against the labeled positives |
| agent + `filter_flagged` | flagged set | precision, recall, false positives per 1000 documents |
| `identifying_snippets` | redaction spans | span validity (verbatim substring of the document), over-redaction ratio |

Retrieval recall bounds everything downstream: a document that never reaches the
candidate list cannot be recovered by any amount of judgment quality. Span
validity matters because a snippet that is not a verbatim substring causes the
Painless script to silently no-op, so a document can be correctly flagged and
still not redacted.

Score each `precision_mode` (0.88 / 0.75 / 0.60) rather than only the default,
and break results out by category where the corpus supports it. On the demo
corpus the decoys vary one marker each (wrong team, wrong seniority, wrong
month, a different named person), so per-category results say considerably more
than an aggregate.

### 7. Report with the assumptions attached

Every number depends on a stated roster, a marker schema, a masking policy, and
a threshold. Publish those alongside the result. A reader who disagrees should
be able to attack a specific input rather than the conclusion as a whole.

## Applying it to Enron

`mail-enron` is already structured for this. `parse_member`
(`scripts/seed_enron.py:118`) indexes `from`, `to`, `cc`, `custodian`, `folder`,
and `message_id` as separate fields, while `message` holds only subject and body
(`:116`). Discovery searches `message`. The naming channel is therefore already
held out by construction.

**Naming channel:** `from`, `to`, `cc`, `custodian`.
**Describing channel:** `message`.

**Roster.** Sweeping every address across the archive reconstructs the
population: `@enron.com` addresses give internal staff, display names give the
alias variants that step 3 needs, and first-and-last-seen timestamps per address
give an approximate active window. A correspondence graph clusters into
something resembling teams. Headers carry no job titles, so attribute coverage
is partial; check what the distribution ships alongside the maildir, and public
sources such as the FERC record cover senior figures.

**Mention recovery.** Pick a subject who is discussed often, take every document
whose `message` contains one of their identifiers as the positive set, mask
those identifiers across the full text including quoted regions, and run the
pipeline against the masked copy. Recall is measured over the positives, false
positives over documents about other people.

**Natural cases.** Role-reference language appears in roughly 1% of Enron
messages and usually describes a generic role or names the person elsewhere in
the same message. Where a thread describes someone in one message and names them
in the reply, the reply supplies a label for genuinely natural indirect language.
Yield is low and the work is manual, but this set is the useful complement to the
masked one, because masking manufactures a document nobody wrote.

**Ethics.** These are real people, most of them private individuals whose mail
became public because of an investigation into others. Report aggregate metrics.
Do not publish reconstructed profiles of individuals, and do not reproduce more
personal data than a finding requires.

## Applying it generically

The naming channel differs by corpus. The method does not.

| Corpus | Naming channel | Describing channel |
| :--- | :--- | :--- |
| Email archive | `From` / `To` / `Cc`, mailbox owner | subject, body |
| Application logs | `user.id`, `actor`, `principal` | free-text message |
| Git history | author, committer, co-author trailers | commit message, code comments |
| Ticketing | reporter, assignee, watchers | description, comments |
| Chat export | `user_id`, mention entities | message text |
| Incident reviews | attendee or participant list | narrative, timeline |

For the roster scheme, define an interface and write adapters rather than
hardcoding a source:

```json
{
  "id": "stable-key",
  "identifiers": ["Jun Tanaka", "j.tanaka@example.com", "EMP-4471"],
  "attributes": {"role": "...", "team": "...", "seniority": "..."},
  "active_from": "2021-06-01",
  "active_to": "2024-03-31"
}
```

An Enron adapter derives entries from headers. A customer's adapter is a CSV
export from HR, a SCIM pull from an identity provider, an on-call schedule, or
`CODEOWNERS` history. In production these systems are what actually resolve a
description like "sole on-call during incident #4091", because the organization
already records who that was.

Provide a null adapter as well. With no roster supplied, the agent estimates
uniqueness by judgment, which is the current behavior. This mirrors how
`discover` degrades to BM25 when no embedding model is deployed and reports the
degradation in `meta.mode`; the evaluation should report which mode produced its
labels for the same reason.

Corpora with no naming channel, such as scanned documents, transcripts, or prose
archives, fall outside this method. There, labels require human assessors.

## What the numbers mean

**They do not transfer.** A recall figure measured on Enron says nothing about
performance on another organization's logs, and it should never be published as
though it did. Detection is probabilistic, and this project does not make
guarantees about redaction coverage.

What the metrics support:

- **Ablations.** "Hybrid retrieval surfaces documents BM25-only misses" is a
  claim about mechanism, and mechanism claims survive a corpus change better
  than performance numbers do.
- **Failure discovery.** A documented failure class, for example markers that
  all match except the time window, is actionable on any data. Negative results
  travel further than positive ones.
- **Regression detection.** Changing the judgment prompt, the embedding model,
  or the thresholds should not silently degrade results. This is the highest
  return use and requires no transfer at all.
- **Threshold calibration.** It replaces "chosen" with "chosen because they sat
  at these operating points on these corpora".

In short: internal validity, not external validity. That is true of most
benchmarks. The intended deliverable is a method a user can run against their
own corpus, plus documented failure modes, not a headline score.

## Limitations

- **Masking manufactures the indirect case.** A sentence written without a name
  would have been phrased differently from one with the name removed, so the
  masked set is a proxy. The natural cases in step 2a's complement are the check
  on this.
- **Anonymity-set labels are model-dependent, not facts.** The roster is a model
  of the population; extracting markers from prose is an interpretation;
  deciding whether a title satisfies a description is a judgment; and treating
  size 1 as the identification threshold is a normative choice, since Recital 26
  asks about means "reasonably likely to be used". The value is that these
  assumptions are explicit, reproducible, and individually contestable, not that
  they are objective.
- **Roster completeness biases in one direction.** Missing contractors, interns,
  or seconded staff make anonymity sets look smaller than they are, turning
  group descriptions into apparent unique identifications.
- **The population is time-scoped.** The set of people fitting a description in
  March is not the set fitting it in May.
- **Anonymity depends on whose knowledge you assume.** A set size computed
  against an internal roster is not the same as one computed against public
  information. State which.
- **Ground truth is a convention.** So is every benchmark label. The question is
  whether the convention is stated, defensible, and reproducible, not whether it
  is true.

## Related

- `knowledge/indirect-identification.md` for why hybrid retrieval is the
  mechanism under test.
- `knowledge/gdpr-reference.md` for the Recital 26 text behind the uniqueness
  criterion.
- `scripts/seed_demo.py` for the synthetic corpus and its labels.
</content>
</invoke>
