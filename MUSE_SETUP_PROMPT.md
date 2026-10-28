# Prompt for a private Muse agent

Copy the prompt from the top of [README.md](README.md) into a Muse agent after granting it access to your **private** `hiddenjob` repository. It is duplicated here so the repository has one short handoff and one operational reference.

## Setup contract

The person using the system provides their own resume and profile to Muse privately. Muse writes only local, ignored `config/profile.json` and `config/automation.json` files and must never commit them, uploaded resumes, evidence, screenshots, cookies, credentials, applications, or complaint records.

## Automation contract

Muse can automate discovery, source capture, resume-based ranking, evidence organization, staging, prefill, status tracking, and follow-up reminders. The default `hands_off_with_final_approval` policy is in `config/automation.example.json`.

## URL-list handoff and source expansion

When a person provides an export from the larger `hiddenjobs` workflow, Muse must save that JSONL only to ignored local configuration:

```bash
hiddenjobs --select url export --jsonl --out config/hiddenjobs-urls.jsonl
```

`config/hiddenjobs-urls.jsonl` is ignored by Git. `hiddenjobs` exports can include job descriptions, contacts, application state, and other private fields even when selecting `url`; do not commit or copy those fields into source configuration. Add the file as `"url_list_file": "hiddenjobs-urls.jsonl"` in a source inside `config/targets.json`. The public importer accepts JSON arrays, `{ "urls": [...] }`, JSONL records with a `url` key, and one URL per line; it retains only URLs. Muse may merge additional direct URLs or explicit sitemap URLs into a source, but must never replace existing sources without preserving them. Use `config/hiddenjobs-targets.example.json` as a non-sensitive starter registry for careers hosts and supported public ATS boards. Copy it to an ignored local file before adding personal priorities; it does not establish that a host has an open job.

At setup, Muse should ask the person once to choose their routine task/site approvals in Muse. It should then perform all approved routine work without repeated interruptions. Before a submission batch, Muse must show the person the exact employers, roles, eligibility rules, and count and request one final approval. After that approval, it sets the project-local environment gate and runs:

```bash
HIDDENJOB_AUTOSUBMIT=1 python3 hiddenjob.py apply-staged \
  --submit --confirm-submit --limit <approved-cap>
```

The agent must stop for login/account creation, passwords, one-time codes, CAPTCHAs, unclear employer questions, missing job evidence, and any result without a confirmation page. It must not send email, make public statements, accept terms, make payments, or file a government complaint without a separate explicit instruction. Muse cannot and must not try to approve its own permissions; the person controls Muse's persistent task/site approvals and the final submission approval.

## Referral note

The README includes the Muse invitation code `YH7I4K`. Have the person redeem it in Muse **Settings** within 48 hours of joining so both people receive 1 billion Muse tokens.
