# GDPR / CCPA reference for erasure

A concise operator reference. Not legal advice — consult your DPO/counsel for
your jurisdiction and obligations.

## GDPR — the right to erasure

- **Article 17 — Right to erasure ("right to be forgotten").** A data subject
  can obtain erasure of personal data concerning them without undue delay in
  defined circumstances (data no longer necessary, consent withdrawn, unlawful
  processing, etc.). Erasure can be satisfied by deletion **or** by
  irreversible anonymization.
- **Recital 26 — what counts as personal data.** Data that can identify a person
  *directly or indirectly* is personal data. Identification "by reference to an
  identifier such as a name, an identification number, location data, an online
  identifier or to one or more factors specific to the physical, physiological,
  genetic, mental, economic, cultural or social identity" — i.e. **contextual /
  indirect identification counts.** This is the specific gap `gdpr-forget-me`
  targets: records that identify a person without their name or ID appearing.
- **Article 5(2) — accountability.** The controller must be able to
  *demonstrate* compliance. This is why every run writes an audit record.
- **Article 30 — records of processing activities.** Maintaining records of
  processing (including erasures) is an obligation for many controllers. The
  local, hash-chained erasure certificates serve this purpose.
- **Article 17(3) — exemptions.** Erasure does not apply where processing is
  necessary for, among others, compliance with a legal obligation, or the
  establishment/exercise/defence of legal claims. Operationally this maps to
  **legal holds** — indices you must NOT erase. Pass them via `--legal-hold`.

## CCPA / CPRA — right to delete

- California consumers can request deletion of personal information a business
  has collected (Cal. Civ. Code §1798.105), with exceptions (completing a
  transaction, security, legal compliance, etc.). The same discover, preview,
  generate-script, run, verify, certificate workflow applies.

## Redaction vs. deletion

| | Redact in place | Hard delete |
|---|---|---|
| Effect | Replace identifying text with `[GDPR_REDACTED]`, keep the record | Remove the whole document |
| Preserves | Operational/observability value, referential integrity, counts | Nothing |
| Best for | Logs/traces where the record is still needed but the person's data must go | Documents that are *about* the subject and have no residual purpose |
| Reversible? | No (original text is overwritten) | No |

Anonymization must be **irreversible** to take the data outside GDPR's scope.
Redacting the identifying snippets (and not storing them elsewhere) achieves
this for the flagged fields.

## Verification & the burden of proof

Because the controller must demonstrate erasure, the generated curl script
includes read-back verification commands for every targeted document, so that
after you run it you can confirm:

- hard delete: the document no longer exists;
- redact: the document exists and contains none of the identifying snippets.

The plan and the exact commands are recorded in a local, hash-chained erasure
certificate written when the script is generated, before anything is applied.
