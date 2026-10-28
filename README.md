# hiddenjob

> ## Give this prompt to Muse
>
> ```text
> Set up this public hiddenjob repository as my personal, private local job-discovery and application assistant.
>
> I will provide my resume and any profile details you need in this private Muse conversation. Create `config/profile.json` and `config/automation.json` from the repository examples. Keep both files, captured postings, application materials, screenshots, cookies, credentials, and case records out of Git. Never use someone else's profile or resume.
>
> First read `README.md`, `MUSE_SETUP_PROMPT.md`, and `skills/hiddenjob/SKILL.md`. I may provide a JSONL export from `hiddenjobs` or another collector. It can contain non-URL fields, so save it only to ignored `config/hiddenjobs-urls.jsonl`; the importer will retain only each `url`. Add it through `url_list_file` in `config/targets.json`. You may add other direct URLs or explicit sitemap URLs without overwriting the existing sources. Use `config/hiddenjobs-targets.example.json` as a public starter registry when planning additional sources; it is not evidence of an open role. Discover postings from publisher-declared robots.txt and sitemaps, capture source evidence, and describe results neutrally. A posting may be less visible or poorly indexed; do not claim a company intentionally hid it without direct evidence. Treat blocked or incomplete sources as unmeasurable, not empty.
>
> Use my resume to rank opportunities and explain why each is a reasonable fit. Keep LCA-like notices separate from roles that appear open for applications. Run the discovery, evidence capture, ranking, staging, ATS prefill, status tracking, and follow-up reminders automatically.
>
> Use the hands-off-with-final-approval policy in `config/automation.json`. Do not interrupt me for routine local work, source research, evidence capture, ranking, staging, or ATS prefill after I grant Muse the relevant task/site access. Batch every qualified, staged application and send me one final approval request listing the employers, roles, and application count before submission. Record `submitted` only after each employer's confirmation page.
>
> Once I approve a batch, set the project-local `HIDDENJOB_AUTOSUBMIT=1` environment gate and use `apply-staged --submit --confirm-submit --limit N` for the already-staged jobs in that approved batch. Stop for any account creation, password, one-time code, CAPTCHA, unclear employer question, missing evidence, missing confirmation page, email/message, public statement, agency complaint, payment, or acceptance of terms. Do not try to bypass Muse's approval system; I control persistent task/site approvals in Muse and final submission approval.
>
> For possible discrimination concerns, centralize evidence in the local draft-case ledger, distinguish EEOC from DOL OFCCP routing, verify the current official agency instructions and deadline, and make clear that a draft is not filed.
> ```
>
> ### Muse invitation
>
> Check out [Muse, your personal AI agent](https://muse.ai/join). Redeem this code in **Settings** within 48 hours of joining and we'll both get **1 billion Muse tokens**:
>
> Code: `YH7I4K`

`hiddenjob` is a private, configuration-first toolkit for discovering job postings that are easy to miss, preserving the source evidence, and organizing a review-first application and accountability ledger.

It starts from publisher-declared `robots.txt` and sitemap URLs rather than relying only on a site's search box. That can surface job pages with opaque URLs, weak indexing, or a separate careers system. It does **not** infer that a company intentionally concealed a job.

## What it does

- Walks declared sitemaps and captures job postings with source URL, capture time, hash, and local evidence copy.
- Labels certain wording as `perm-like`, `lca-like`, or `recruitment-notice-like` for human review. These are content labels, not legal determinations.
- Keeps a local application ledger: `staged`, `drafted`, `submitted`, `interview`, `offer`, `rejected`, `withdrawn`, or `closed`.
- Optionally prefills a staged ATS form through a user-installed local browser bridge. It uses a gitignored profile and stops for review by default.
- Keeps a separate local draft-case ledger for EEOC and DOL OFCCP routing, with captured evidence links and official filing instructions.
- Keeps personal configuration, evidence, resumes, application materials, and case records out of Git by default.

It deliberately does **not** send email, create accounts, solve CAPTCHAs, contact employers, or file complaints. The optional browser adapter only fills a reviewed ATS form by default. Its submit path requires `--submit`, `--confirm-submit`, `HIDDENJOB_AUTOSUBMIT=1`, and a real post-submit confirmation.

## Quick start

```bash
git clone git@github.com:YOUR-ACCOUNT/hiddenjob.git
cd hiddenjob
python3 hiddenjob.py init
# Edit config/targets.json before any network request.
python3 hiddenjob.py sync --limit 25
python3 hiddenjob.py jobs --classification perm-like
python3 hiddenjob.py stage 'https://example.com/jobs/123' --note 'Reviewed source and fit'
python3 hiddenjob.py apply 'https://example.com/jobs/123' --dry-run
python3 hiddenjob.py apply-staged --limit 3 --dry-run
python3 hiddenjob.py report
```

Run the standard-library test suite:

```bash
python3 -m unittest discover -s tests -v
```

## Import URLs from hiddenjobs, Muse, or another collector

`sync` accepts direct job URLs in addition to robots/sitemap discovery. This is the recommended handoff from the larger `hiddenjobs` workflow: export its JSONL into ignored local configuration. The current export may contain other fields; this importer reads and retains only `url`.

```bash
# On a machine with the hiddenjobs CLI, create a local JSONL handoff.
hiddenjobs --select url export --jsonl --out config/hiddenjobs-urls.jsonl

# Start from the example, then add a source like this to config/targets.json.
cp config/url-list.example.jsonl config/hiddenjobs-urls.jsonl
```

```json
{
  "name": "hiddenjobs-import",
  "url_list_file": "hiddenjobs-urls.jsonl",
  "sitemap_urls": ["https://careers.example.com/sitemap.xml"],
  "job_url_pattern": "/jobs/"
}
```

`url_list_file` is relative to `config/`. It can be a JSON array of URLs, a JSON object with a `urls` array, JSON Lines records with a `url` field (including `hiddenjobs export --jsonl`), or one URL per line. The importer validates HTTP(S) URLs, deduplicates them, and retains only the URL—not resume details, contacts, descriptions, or any other fields in an external record. Inline `urls` work too. `sitemap_urls` is optional and supplements `robots.txt`; apply `job_url_pattern` and/or `job_url_regex` to keep sitemap crawling focused. If `--limit` leaves candidates unvisited, `sync` prints `truncated=True`; that is a bounded run, not a negative result.

The public [starter registry](config/hiddenjobs-targets.example.json) contains the careers hosts and known public ATS boards currently used by the owner’s `hiddenjobs` target list. It is a planning aid, not a claim of an open role or a request to crawl every host. Muse may add more records to an ignored local copy, then activate only the direct URLs or sitemap URLs it can measure.

## ATS boards and company hosts

Set `"ats_targets_file": "hiddenjobs-targets.json"` in `config/targets.json` (after copying the example registry to that ignored local file) and `sync` will also pull every registry entry's public ATS board:

- Supported board kinds: `greenhouse` (`boards-api.greenhouse.io`), `ashby` (`api.ashbyhq.com/posting-api`), `lever` (`api.lever.co`), `smartrecruiters` (`api.smartrecruiters.com`), `workable` (`apply.workable.com/api/v1/widget`), `personio` (`{company}.jobs.personio.com/xml`). One HTTP call per board; the JSON/XML response is saved as evidence per posting. SmartRecruiters and Workable listing endpoints omit full descriptions, so those records carry title/location/department text and link to the live posting.
- Entries with `enabled: false`, a missing `kind`/`board`, or an unsupported kind are skipped with a stderr note — never silently and never treated as "no jobs".
- Board slugs must be verified before they are added: probe the board's public API and keep only entries that return HTTP 200 with real postings.

Entries under `company_hosts` with `"enabled": true` are walked through that host's own `robots.txt`/sitemap with a bounded budget (`--max-host-sitemap-urls`, default 400), preferring career child maps and filtering URLs to job/career/position/opening paths. Hosts default to disabled; enable only hosts whose sitemaps are publisher-declared and measurable.

## Feed sources (no sitemap needed)

These `kind:` entries under `sources` pull from public job APIs instead of sitemaps:

- `hackernews` — Hacker News job stories via the public Firebase API.
- `remoteok` — RemoteOK's public API (startup/tech-heavy remote board; needs a browser-like User-Agent, handled internally). Their API terms require a follow (non-nofollow) link-back to remoteok.com wherever their data is displayed.
- `remotive` — Remotive's public API (hand-curated remote board).
- `arbeitnow` — Arbeitnow's public API (supplemental; heavily German/EU-leaning; they ask for a link-back).
- `usajobs` — USAJOBS federal search API. Requires a free API key: sign up at https://api.data.gov/signup/ (USAJOBS accepts api.data.gov keys), then export it before syncing:

```bash
export USAJOBS_API_KEY="your-key-here"
```

The key is read from the env var named by the source's `api_key_env` (default `USAJOBS_API_KEY`) and is never written to the repo. Without a key the source is skipped with a stderr note. Optionally set `"keyword"` on the source to filter (e.g. `"keyword": "product manager"`).

## Evidence states

| State | Meaning |
| --- | --- |
| Captured | The tool fetched the URL now, saved a local copy and hash, and recorded a time. |
| Unmeasurable | The host returned a block, challenge, error, or malformed sitemap. This is not evidence that it has no jobs. |
| Staged | A person reviewed the source and wants to consider applying. No application was sent. |
| Submitted | Record only after an actual confirmation from the employer's system. |
| Draft complaint case | A private evidence record, not a filed agency complaint. |

## Optional browser prefill

Copy `config/profile.example.json` to the ignored `config/profile.json`, add only the fields you want to offer to forms, and install a compatible local `fcdp` bridge. The adapter can validate its planned action without a browser:

```bash
python3 hiddenjob.py apply 'https://example.com/jobs/123' --dry-run
```

After reviewing the open form and evidence, a user who has separately enabled the environment gate can request submission. Account creation, passwords, and CAPTCHAs always stop for the user; a click without a confirmation page is recorded as unverified.

`apply-staged --limit N` provides the repeatable automation lane: it processes only the bounded set of jobs that the person already staged, preserving one result record per job. It inherits the same prefill-by-default and submission gates.

## EEOC and DOL OFCCP

The complaint ledger prevents application records and potential agency matters from being mixed together. It keeps two routes distinct:

- **EEOC** addresses protected-basis employment discrimination generally. The current official [charge process](https://www.eeoc.gov/how-file-charge-employment-discrimination) says filing deadlines are generally 180 days and may be 300 days in some jurisdictions.
- **DOL OFCCP** accepts complaints concerning disability or protected-veteran discrimination by covered federal contractors or subcontractors. Its current official [complaint process](https://www.dol.gov/agencies/ofccp/contact/file-complaint) states a 300-calendar-day deadline and explains that a pre-complaint inquiry does not extend it.

Create a local draft and attach already-captured evidence:

```bash
python3 hiddenjob.py complaint create \
  --agency ofccp --employer 'Example Employer' \
  --alleged-date 2026-09-01 --contractor-status unknown
python3 hiddenjob.py complaint evidence --case 1 --url 'https://example.com/jobs/123'
python3 hiddenjob.py complaint list
```

This is organizational support, not legal advice or a filing service. See [the routing guide](docs/complaint-routing.md), verify the official pages again before acting, and preserve the source evidence unchanged.

## Muse handoff

The copy-ready prompt above is the fastest setup path. The fuller operational handoff is in [MUSE_SETUP_PROMPT.md](MUSE_SETUP_PROMPT.md): it accepts the person's private resume/profile, automates discovery and preparation, and supports limited auto-submit only after that person explicitly chooses the mode and its cap.

## Privacy boundary

Before a commit, run:

```bash
git grep -n -E -i '(@[^[:space:]]+\.(com|org|net)|phone|resume|linkedin|token|api[_-]?key|address)' || true
git status --short
```

Review every match. The examples use placeholders only; do not commit a real profile or `.hiddenjob/` data directory.

## License

[MIT](LICENSE)
