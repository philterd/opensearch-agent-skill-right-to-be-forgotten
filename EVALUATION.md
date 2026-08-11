# Evaluating indirect identification

How to build ground truth for the hard half of an erasure request, what two real
corpora measured, and what the numbers do and do not support.

## TL;DR

**The idea.** Find a corpus with two channels: a structured one that names
people (email headers, a case caption) and an unstructured one that describes
them. Discovery only searches the second, so the first can supply labels without
contaminating the search.

Pick a subject. Record every document whose text contains one of their
identifiers, then redact those identifiers and index the result separately. The
label is a fact rather than a judgment: the string was there before it was
removed. Whether the pipeline still finds those documents measures whether the
residual context identifies the person.

**The disciplines that keep it honest.**

- *Redact broadly, label narrowly.* Remove anything that might refer to the
  subject, but count a document as a positive only on a variant no one else in
  the population shares. A given name is not evidence about a particular person.
- *The leakage audit is a gate, not a warning.* If one variant survives, or a
  document id embeds a name, or the redaction leaves a marker that appears only
  in positives, the run fails and produces no score.
- *The profile comes from held-out content.* Build the query from one half of a
  subject's records and score retrieval of the other, so no document both writes
  the query and is measured by it.
- *Score the three stages separately.* Retrieval bounds everything downstream,
  and one end-to-end number hides which stage failed.
- *Score across subjects* where a corpus gives one document per person, since
  recall within such a subject is 0% or 100%.

**What it cannot escape.** Redacting a name manufactures the indirect case: a
sentence written without a name would have been phrased differently from one
with the name taken out. These are proxies, useful for ablations, failure
discovery, regression detection and threshold calibration. Results do not
transfer between corpora, which is why `assess` measures the corpus in front of
you rather than quoting a number from this one.

## Why this needs a document

The direct pass validates itself: `discover-direct` finds documents containing a
literal identifier, and anyone can confirm a hit by searching for the same
string.

The indirect pass has no such key. When a document says *"the sole senior
frontend engineer on Checkout who resigned in March"*, whether that identifies
someone is not a property of the document. It depends on how many people fit,
which the document does not say. Producing a key by hand means an assessor
reading every document, which does not reproduce, does not scale, and on a real
corpus means performing the identification the tool exists to remediate.

## Methodology

**Two channels.** Most corpora have a *naming channel* that identifies people in
structured form (an email header, a `user.id`, a case caption) and a *describing
channel* of unstructured text. Discovery searches the describing channel, so the
naming channel can supply labels without contaminating the search. Confirm the
two do not overlap, or the evaluation is circular.

Check what a field holds rather than what it is called. **Measured:** reading
Enron's `From`/`To`/`Cc` gave display names for 1.7% of addresses; the names are
in `X-From`/`X-To`/`X-cc`, and reading those gave 90% on the same data.

**Labels by mention recovery.** For a subject, the positives are the documents
whose text contained one of their identifiers. Mask those identifiers, evaluate
on what remains. The label is a fact: the string was there before removal.

**Mask broadly, label narrowly.** One variant list cannot do both jobs. Masking
must remove anything that might refer to the subject, because a survivor makes
the document trivially retrievable. Labelling must use only variants no one else
produces. **Measured:** seven people in the Enron corpus are called Harry, and
labelling one of them on the full variant list made 92% of their positives
documents about somebody else.

**A mention is not a description.** Most documents containing a name contain it
in a recipient list or a signature. Masking those yields a document about
nobody. Classify each mention as prose or list context, and count only the prose
when deciding whether a subject can be scored.

**The leakage audit is a gate.** If one variant survives, every number below it
is fiction, so the run fails rather than warns. It checks surviving variants,
readable document ids, header fields carried across, signature-block phone
numbers under a recorded policy, and the mask marker itself. Masking therefore
removes the variant and leaves nothing behind: **measured**, a `[MASKED]` token
appeared in 743 of 517,394 documents against 711 masked, so searching for it
returned the answer key.

**Profiles come from content, not metadata.** **Measured:** a profile built from
a subject's active window and top correspondents returned 0 of 451 positives at
k=500, and pointed at the wrong domain entirely, because header co-occurrence is
dominated by distribution lists. A profile describing what they produced
returned 12% to 15%. Derive it from a held-out half of the positives so no
document both writes the query and is scored by it, and generate several
wordings: **measured**, three wordings varied threefold in recall@50.

**Score across subjects where a corpus gives one document per person.** Recall
within such a subject is 0% or 100%. Split each record in half, mask both,
derive the profile from one half and score retrieval of the other.

## The two datasets

**Enron email**, 517,394 messages across 150 custodians, fetched from CMU at run
time. Chosen because the naming channel is already held out: headers are indexed
separately from `message`, which is what discovery searches.

**US case law**, from the CourtListener bulk export, streamed as a byte range and
cached. Chosen after Enron failed, because an opinion's purpose is to recount
what a person did while calling them "the defendant", and the caption names them
in a separate field.

A synthetic corpus (`seed_demo.py`) acts as the control. Its documents are
written to describe people and its decoys vary one marker each, so it is where
judgment quality is measurable. Its labels are hand-authored, so it proves
nothing about real data.

## Results

### Enron fails on the describing channel

| | |
| :--- | :--- |
| descriptive references per document | 0.20 (14.4% of documents carry one) |
| roster | 87,479 addresses, 58% with a display name, 11% with an Exchange login |
| a well-chosen subject | 451 positives; 143 of the 219 in the score half were recipient-list mentions |
| retrieval, best case | 31.6% recall@500 against 76 descriptive positives |
| judgment | flagged nothing at the default threshold, one useful document at the loosest |

Role-reference language appears in 0.39% of messages, and sampling it found
those mentions either name the person in the same sentence, describe a generic
role, or describe someone with no roster entry. Both routes are closed: masked
mention recovery fails structurally, natural cases fail on yield.

Enron remains valuable for the direct pass, for scale, and for regression. Its
517,394 real messy documents exposed four defects the synthetic corpus never
would.

### US case law is usable

**Measured** on a 40Mb slice, using the caption parser and the alias expansion
and removal masking the harness uses:

| | |
| :--- | :--- |
| descriptive references per document | 21.0 (98.7% of documents) |
| opinions carrying a party-role reference | 3,439, all joined to a caption |
| yielding a person party | 2,804 (82%); the rest are institution against institution |
| alias variants per subject | 7.8 mean |
| party also named in the body | 87%, so masking is required |
| still a description after masking | 76%, median 14,170 characters |
| distinct party surnames | 2,380, of which 81% appear in exactly one opinion |

The 76% holding steady while masking went from one surname to 7.8 variants is
the load-bearing result: these descriptions survive an aggressive mask.

### Retrieval, and a fusion defect

**Measured** on 2,692 case-law subjects over a 5,470-document index, hit-rate at
k. Hit-rate is not comparable across corpus sizes, so the index size belongs
beside every figure:

| | @1 | @5 | @10 | @25 | @50 | MRR |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| hybrid, reciprocal rank fusion | 7.7% | 38.9% | 44.8% | 50.0% | 53.1% | 0.207 |
| BM25 only | 32.7% | 44.0% | 47.0% | 50.0% | 50.9% | 0.376 |

Intervals separate only at k=1 and k=5, where BM25 wins. From k=10 on the two
are statistically indistinguishable: 44.8% against 47.0% at k=10, and 53.1%
against 50.9% at k=50, all overlapping.

**Fusion.** Min-max normalization scales each clause within its own result set,
so an uninformative neural clause has its noise stretched across the full range
and averaged into the lexical score, pushing good lexical hits down. Reciprocal
rank fusion uses ranks, so a weak clause contributes bounded noise. **Measured**
on 300 subjects, normalization gave hybrid 5.7% at k=5 against RRF's 35.3%, and
on Enron the same change lifted hybrid from 22.4% to 31.6% at top-k. RRF is the
default because that defect is real and large.

**The mechanism claim is still unsupported.** An earlier run on 300 subjects
showed hybrid ahead by 7.0 points at k=50 and read as the first evidence that
hybrid surfaces what BM25 misses. Nine times the subjects shrank that to 2.2
points with overlapping intervals, and reversed the ordering at k=10. The
apparent advantage was sampling noise. What RRF buys is parity, not an edge:
prefer lexical for precision at the top, where BM25 leads by 25 points at k=1,
and treat the two as equivalent at depth.

This is also the clearest argument in this document for stating intervals and
resisting a result until it separates. The 7-point gap was reported with the
caveat that it was not settled; it was not, and it did not survive.

### Judgment, on the synthetic corpus only

| threshold | precision | recall | false positives |
| :--- | ---: | ---: | ---: |
| strict_precision (0.88) | 1.00 | 0.91 | 0 |
| balanced (0.75) | 1.00 | 0.91 | 0 |
| high_recall (0.60) | 0.91 | 0.91 | 1 |

All eight decoy categories scored a zero false-positive rate, including
near-misses differing by one marker. Spans were 11 of 11 verbatim, and
over-redaction averaged 7.2% of a document, peaking at 58.9%.

## What an ideal dataset looks like

1. **Both channels.** Prose that describes people, and a structured record of
   who each description is about, held out of the searched text. Most candidates
   fail here: a corpus describing people richly has usually had their names
   removed on purpose, and one with a clean naming channel is usually a record
   of what was sent rather than of who anyone is.
2. **People as subject matter**, not as an actor field. This is the difference
   between 21.0 and 0.20 references per document, and it follows from what each
   kind of record is for.
3. **Descriptions that occur without the name naturally**, so masking is a
   convenience rather than the thing that manufactures the case.
4. **Many documents per person**, because the capability under test is finding a
   scattered footprint. Case law gives 81% of people exactly one document.
5. **A bounded population**, so anonymity-set size is computable and the
   uniqueness test has an answer.
6. **A use case that applies.** Nobody files an erasure request against a
   published court opinion.
7. **No re-identification prohibition.** Clinical records fit criteria 1 to 5
   better than anything else considered, and their data-use agreements forbid
   precisely what this harness measures.

Nothing tested satisfies all seven. Operational records, the logs, tickets and
incident reviews the skill targets, would satisfy most, and no public example
with an intact naming channel has been found.

## Metrics

| Stage | Metric | Why |
| :--- | :--- | :--- |
| corpus | descriptive references per document | decides whether anything is findable at all; percent-of-documents does not separate corpora, density does |
| `discover` | recall@k, or hit-rate@k across subjects | one document per person makes per-subject recall meaningless |
| `discover` | mean reciprocal rank | catches two modes that find the same documents and order them differently |
| `discover` | BM25-only ablation | tests the mechanism claim rather than assuming it |
| judgment | precision, recall, false positives per 1000 | at every threshold from one pass, since thresholds reread the same confidence scores |
| judgment | per-decoy-category false positives | an aggregate hides which marker the judgment cannot tell apart |
| snippets | span validity | a snippet that is not a verbatim substring makes the Painless update silently no-op |
| snippets | over-redaction ratio | the cost of a false positive under `redact_in_place` |

Report the three stages separately: retrieval recall bounds everything
downstream, and one end-to-end number hides which stage failed. Attach a Wilson
interval to every rate. **Measured:** 24 of 76 is 31.6% with an interval from
22.2 to 42.7, which reads far firmer without it. Compare modes across k, not at
the largest k alone, or a run that converges at k=50 while differing by 33
points at k=5 reads as a tie. State the assumptions each number rests on:
subject, alias list, masking policy, k, and thresholds.

## Summary

**What is supported.** The machinery works. Retrieval, score fusion after the
RRF fix, agent judgment, masking and the leakage gate all measure well.
Discrimination is clean on documents written to describe people, and hybrid
retrieval beats BM25 at depth on real documents.

**What is not.** That a customer's corpus contains the text this needs. The two
corpora measured sit near the extremes: court opinions close to a best case,
Enron email close to a worst case, and nothing measured between them. Every
precision figure comes from synthetic data. Case law retrieves well but has
never been through a judgment pass, which is the largest remaining gap and the
cheapest to close.

**Standing caveats.** Masking manufactures the indirect case in both corpora, so
these are proxies for naturally occurring indirect reference. Queries are
generated term bags, which favour lexical matching: **measured** on Enron, fluent
prose narrowed BM25's advantage from 9.2 points to 1.3 and lowered both modes,
and a real request arrives as prose. The host agent both produces the judgments
and reports the score, which the answer-key discipline constrains but does not
remove. Results do not transfer: a figure measured here says nothing about
another organization's data.

**What follows.** Report which capability a number evidences and which it does
not. The supported claim is that the pipeline finds people where the corpus
describes them, not that corpora generally describe them. `assess` exists
because that question can only be answered per corpus, and answering it on the
reader's own index is worth more than any figure in this document.

## Related

- `knowledge/indirect-identification.md` for why hybrid retrieval is the
  mechanism under test.
- `knowledge/gdpr-reference.md` for the Recital 26 text behind the uniqueness
  criterion.
- `scripts/seed_demo.py` for the synthetic corpus and its labels.
