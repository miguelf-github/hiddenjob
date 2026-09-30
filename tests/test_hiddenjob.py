import argparse
import importlib.util
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("hiddenjob", Path(__file__).parents[1] / "hiddenjob.py")
hiddenjob = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hiddenjob)


class HiddenjobTests(unittest.TestCase):
    def test_perm_like_marker_classification(self):
        result = hiddenjob.classify("Minimum Requirements. Foreign equivalent accepted. Please send resumes. Job duties include system design.")
        self.assertEqual(result["classification"], "perm-like")
        self.assertGreaterEqual(result["perm_score"], 5)


    def test_normal_job_is_not_visa_classified(self):
        result = hiddenjob.classify("Build reliable services with a collaborative engineering team.")
        self.assertEqual(result["classification"], "unclassified")


    def test_sitemap_leaf_and_index_parsing(self):
        children, leaves = hiddenjob.sitemap_entries("<sitemapindex><sitemap><loc>https://example.test/jobs.xml</loc></sitemap></sitemapindex>")
        self.assertEqual(children, ["https://example.test/jobs.xml"]); self.assertFalse(leaves)
        children, leaves = hiddenjob.sitemap_entries("<urlset><url><loc>https://example.test/jobs/1</loc></url></urlset>")
        self.assertFalse(children); self.assertEqual(leaves, ["https://example.test/jobs/1"])

    def test_ordinary_captcha_word_is_not_a_challenge(self):
        self.assertFalse(hiddenjob.is_challenge_page("This job describes a CAPTCHA accessibility option.".lower()))
        self.assertTrue(hiddenjob.is_challenge_page("<title>Just a moment...</title> cf-chl-xyz".lower()))

    def test_jobsnow_job_path_excludes_non_posting_page(self):
        pattern = r"^/jobs/\d+(?:[-/]|$)"
        self.assertIsNotNone(hiddenjob.re.search(pattern, "/jobs/640082644-software-engineer"))
        self.assertIsNone(hiddenjob.re.search(pattern, "/jobs/apply-by-mail"))

    def test_ats_dry_run_uses_private_profile_without_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); profile = root / "profile.json"
            profile.write_text(json.dumps({"full_name":"Example Name","email":"example@example.test","phone":"+1-555-555-0100","resume_path":"/tmp/example.pdf"}))
            result = subprocess.run(["python3", str(Path(__file__).parents[1] / "ats_prefill.py"), "--url", "https://example.test/jobs/1", "--profile", str(profile), "--evidence-dir", str(root / "evidence"), "--dry-run"], text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout)["outcome"], "planned")

    def test_read_url_list_accepts_hiddenjobs_jsonl_and_discards_other_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "hiddenjobs-urls.jsonl"
            path.write_text('{"url":"https://example.test/jobs/1","email":"private@example.test"}\n{"url":"https://example.test/jobs/2","description":"private text"}\n')
            self.assertEqual(hiddenjob.read_url_list(path), ["https://example.test/jobs/1", "https://example.test/jobs/2"])

    def test_sync_combines_inline_and_file_urls_without_duplicates(self):
        page = '<script type="application/ld+json">{"@type":"JobPosting","title":"Engineer","description":"Build services"}</script>'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "config").mkdir(); config = root / "config" / "targets.json"; imports = root / "config" / "urls.jsonl"
            imports.write_text('{"url":"https://example.test/jobs/2","email":"private@example.test"}\n')
            config.write_text(json.dumps({"data_dir":"ledger", "sources":[{"name":"import", "urls":["https://example.test/jobs/1", "https://example.test/jobs/2"], "url_list_file":"urls.jsonl"}]}))
            args = argparse.Namespace(config=str(config), limit=10, max_sitemap_urls=10)
            with mock.patch.object(hiddenjob, "fetch", side_effect=lambda url: (page, url)) as fetch:
                self.assertEqual(hiddenjob.cmd_sync(args), 0)
            self.assertEqual(fetch.call_count, 2)
            con = sqlite3.connect(root / "ledger" / "hiddenjob.sqlite3")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)

    def test_sync_honors_per_source_limit_override(self):
        page = '<h1>Engineer</h1><p>Build services</p>'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "config").mkdir(); config = root / "config" / "targets.json"
            config.write_text(json.dumps({"data_dir": "ledger", "sources": [
                {"name": "deep", "limit": 3,
                 "urls": ["https://example.test/jobs/1", "https://example.test/jobs/2",
                          "https://example.test/jobs/3", "https://example.test/jobs/4"]},
                {"name": "shallow",
                 "urls": ["https://example.test/jobs/5", "https://example.test/jobs/6",
                          "https://example.test/jobs/7", "https://example.test/jobs/8"]},
            ]}))
            args = argparse.Namespace(config=str(config), limit=2, max_sitemap_urls=10)
            with mock.patch.object(hiddenjob, "fetch", side_effect=lambda url: (page, url)):
                self.assertEqual(hiddenjob.cmd_sync(args), 0)
            con = sqlite3.connect(root / "ledger" / "hiddenjob.sqlite3")
            deep = con.execute("SELECT COUNT(*) FROM jobs WHERE source='deep'").fetchone()[0]
            shallow = con.execute("SELECT COUNT(*) FROM jobs WHERE source='shallow'").fetchone()[0]
            self.assertEqual(deep, 3)
            self.assertEqual(shallow, 2)

    def test_sync_take_tail_selects_newest_candidates(self):
        # Publishers whose sitemap lists oldest-first (jobs.now numeric IDs
        # ascend with newness): take:"tail" must capture the newest, not oldest.
        page = '<h1>Engineer</h1><p>Build services</p>'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "config").mkdir(); config = root / "config" / "targets.json"
            config.write_text(json.dumps({"data_dir": "ledger", "sources": [
                {"name": "oldest_first", "limit": 2, "take": "tail",
                 "urls": ["https://example.test/jobs/1", "https://example.test/jobs/2",
                          "https://example.test/jobs/3", "https://example.test/jobs/4"]},
            ]}))
            args = argparse.Namespace(config=str(config), limit=10, max_sitemap_urls=10)
            fetched_urls = []
            def fake_fetch(url):
                fetched_urls.append(url)
                return (page, url)
            with mock.patch.object(hiddenjob, "fetch", side_effect=fake_fetch):
                self.assertEqual(hiddenjob.cmd_sync(args), 0)
            self.assertEqual(fetched_urls,
                             ["https://example.test/jobs/3", "https://example.test/jobs/4"])

    def test_sync_skips_source_when_all_seeds_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "config").mkdir(); config = root / "config" / "targets.json"
            config.write_text(json.dumps({"data_dir": "ledger", "sources": [
                {"name": "flaky", "robots_url": "https://example.test/robots.txt",
                 "sitemap_fallback": "https://example.test/sitemap.xml", "job_url_pattern": "/jobs/"},
                {"name": "steady", "urls": ["https://example.test/jobs/9"]},
            ]}))
            page = '<h1>Engineer</h1><p>Build services</p>'
            args = argparse.Namespace(config=str(config), limit=10, max_sitemap_urls=10)
            def fake_fetch(url):
                if "robots.txt" in url:
                    raise hiddenjob.FetchBlocked("https://example.test/robots.txt: timed out")
                return (page, url)
            with mock.patch.object(hiddenjob, "fetch", side_effect=fake_fetch):
                self.assertEqual(hiddenjob.cmd_sync(args), 0)
            con = sqlite3.connect(root / "ledger" / "hiddenjob.sqlite3")
            self.assertEqual(con.execute("SELECT url FROM jobs").fetchone()[0], "https://example.test/jobs/9")

    def test_sync_uses_explicit_sitemap_urls_without_robots(self):
        sitemap = '<urlset><url><loc>https://example.test/jobs/1</loc></url><url><loc>https://example.test/about</loc></url></urlset>'
        page = '<h1>Engineer</h1><p>Build services</p>'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "config").mkdir(); config = root / "config" / "targets.json"
            config.write_text(json.dumps({"data_dir":"ledger", "sources":[{"name":"sitemap-import", "sitemap_urls":["https://example.test/jobs.xml"], "job_url_pattern":"/jobs/"}]}))
            args = argparse.Namespace(config=str(config), limit=10, max_sitemap_urls=10)
            with mock.patch.object(hiddenjob, "fetch", side_effect=lambda url: (sitemap, url) if url.endswith(".xml") else (page, url)):
                self.assertEqual(hiddenjob.cmd_sync(args), 0)
            con = sqlite3.connect(root / "ledger" / "hiddenjob.sqlite3")
            self.assertEqual(con.execute("SELECT url FROM jobs").fetchone()[0], "https://example.test/jobs/1")

    def test_sync_still_rejects_source_with_no_urls_configured(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "config").mkdir(); config = root / "config" / "targets.json"
            config.write_text(json.dumps({"data_dir": "ledger", "sources": [{"name": "empty"}]}))
            args = argparse.Namespace(config=str(config), limit=10, max_sitemap_urls=10)
            with self.assertRaises(SystemExit):
                hiddenjob.cmd_sync(args)

    def test_ats_greenhouse_normalizes_postings(self):
        payload = {"jobs": [{"absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1", "title": "Product Manager",
                             "updated_at": "2026-09-20T00:00:00Z", "content": "<p>Own the roadmap.</p>"}]}
        with mock.patch.object(hiddenjob, "fetch_json", return_value=payload):
            postings = hiddenjob.ats_board_postings("greenhouse", "acme", "Acme")
        self.assertEqual(len(postings), 1)
        self.assertEqual(postings[0]["url"], "https://job-boards.greenhouse.io/acme/jobs/1")
        self.assertEqual(postings[0]["title"], "Product Manager")
        self.assertEqual(postings[0]["company"], "Acme")
        self.assertIn("Own the roadmap.", postings[0]["description"])

    def test_ats_ashby_normalizes_postings(self):
        payload = {"jobs": [{"id": "abc", "jobUrl": "https://jobs.ashbyhq.com/acme/abc", "title": "IT Support Engineer",
                             "publishedAt": "2026-09-19T00:00:00Z", "descriptionHtml": "<p>Help users.</p>"}]}
        with mock.patch.object(hiddenjob, "fetch_json", return_value=payload):
            postings = hiddenjob.ats_board_postings("ashby", "acme", "Acme")
        self.assertEqual(len(postings), 1)
        self.assertEqual(postings[0]["url"], "https://jobs.ashbyhq.com/acme/abc")
        self.assertIn("Help users.", postings[0]["description"])

    def test_ats_lever_normalizes_postings(self):
        payload = [{"hostedUrl": "https://jobs.lever.co/acme/xyz", "text": "Forward Deployed Engineer",
                    "createdAt": 1758758400000, "description": "<p>Work with customers.</p>"}]
        with mock.patch.object(hiddenjob, "fetch_json", return_value=payload):
            postings = hiddenjob.ats_board_postings("lever", "acme", "Acme")
        self.assertEqual(len(postings), 1)
        self.assertEqual(postings[0]["title"], "Forward Deployed Engineer")
        self.assertTrue(postings[0]["date_posted"].startswith("2025-09-25"))

    def test_ats_unsupported_kind_is_refused(self):
        with self.assertRaises(hiddenjob.FetchBlocked):
            hiddenjob.ats_board_postings("linkedin", "1337", "LinkedIn")

    def test_ats_timeout_passes_through_to_fetch(self):
        payload = {"jobs": []}
        with mock.patch.object(hiddenjob, "fetch_json", return_value=payload) as fetch:
            hiddenjob.ats_board_postings("greenhouse", "acme", "Acme", timeout=7)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs.get("timeout"), 7)

    def test_host_sources_only_enables_explicit_hosts(self):
        cfg = {"company_hosts": [{"company": "On", "host": "careers.on.test", "enabled": True},
                                 {"company": "Off", "host": "careers.off.test", "enabled": False},
                                 {"company": "Default", "host": "careers.default.test"}]}
        sources = hiddenjob.host_sources(cfg)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["name"], "host:careers.on.test")
        self.assertEqual(sources[0]["robots_url"], "https://careers.on.test/robots.txt")
        self.assertIn("job_url_regex", sources[0])

    def test_sync_pulls_ats_targets_and_skips_unsupported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "config").mkdir()
            config = root / "config" / "targets.json"
            registry = root / "config" / "targets-local.json"
            registry.write_text(json.dumps({"targets": [
                {"company": "Acme", "ats": {"kind": "ashby", "board": "acme"}},
                {"company": "Stale", "ats": {"kind": "ashby", "board": "stale"}, "enabled": False},
                {"company": "Nope", "ats": {"kind": "linkedin", "board": "1337"}},
            ]}))
            config.write_text(json.dumps({"data_dir": "ledger", "sources": [], "ats_targets_file": "targets-local.json"}))
            postings = [{"url": "https://jobs.ashbyhq.com/acme/1", "title": "Engineer", "company": "Acme",
                         "date_posted": "", "description": "Build services", "evidence_text": "{}"}]
            args = argparse.Namespace(config=str(config), limit=10, max_sitemap_urls=10)
            def fake_boards(kind, board, company, timeout=15):
                if kind == "linkedin":
                    raise hiddenjob.FetchBlocked("unsupported ATS board kind: linkedin")
                return postings
            with mock.patch.object(hiddenjob, "ats_board_postings", side_effect=fake_boards):
                self.assertEqual(hiddenjob.cmd_sync(args), 0)
            rows = sqlite3.connect(root / "ledger" / "hiddenjob.sqlite3").execute("SELECT url, source FROM jobs").fetchall()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row[0], "https://jobs.ashbyhq.com/acme/1")
            self.assertEqual(row[1], "ats:ashby:acme")
            con = sqlite3.connect(root / "ledger" / "hiddenjob.sqlite3")
            self.assertTrue(con.execute("SELECT evidence_path FROM jobs").fetchone()[0].endswith(".json"))

    def test_sync_ats_keyword_filter_keeps_only_matching_titles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "config").mkdir()
            config = root / "config" / "targets.json"
            registry = root / "config" / "targets-local.json"
            registry.write_text(json.dumps({"targets": [
                {"company": "Acme", "ats": {"kind": "ashby", "board": "acme"},
                 "keywords": ["data annotator", "data quality"]},
            ]}))
            config.write_text(json.dumps({"data_dir": "ledger", "sources": [], "ats_targets_file": "targets-local.json"}))
            postings = [
                {"url": "https://jobs.ashbyhq.com/acme/1", "title": "Data Annotator", "company": "Acme",
                 "date_posted": "", "description": "Label things", "evidence_text": "{}"},
                {"url": "https://jobs.ashbyhq.com/acme/2", "title": "Backend Engineer", "company": "Acme",
                 "date_posted": "", "description": "Build services", "evidence_text": "{}"},
                {"url": "https://jobs.ashbyhq.com/acme/3", "title": "DATA QUALITY Lead", "company": "Acme",
                 "date_posted": "", "description": "Own quality", "evidence_text": "{}"},
            ]
            args = argparse.Namespace(config=str(config), limit=10, max_sitemap_urls=10)
            with mock.patch.object(hiddenjob, "ats_board_postings", return_value=postings):
                self.assertEqual(hiddenjob.cmd_sync(args), 0)
            rows = sqlite3.connect(root / "ledger" / "hiddenjob.sqlite3").execute("SELECT title FROM jobs").fetchall()
            titles = sorted(r[0] for r in rows)
            self.assertEqual(titles, ["DATA QUALITY Lead", "Data Annotator"])


RANK_SPEC = importlib.util.spec_from_file_location("hiddenjob_rank", Path(__file__).parents[1] / "tools" / "hiddenjob_rank.py")
ranker = importlib.util.module_from_spec(RANK_SPEC)
RANK_SPEC.loader.exec_module(ranker)


class RankerGateTests(unittest.TestCase):
    """Regression gates for ranker false positives triaged 2026-09-26."""

    def tier(self, title, company="Acme", desc="", loc="", classification="unclassified"):
        return ranker.classify(title, company, desc, loc, classification)["tier"]

    def test_new_grad_pipeline_excluded(self):
        self.assertEqual(
            self.tier("Associate Product Manager, New Grad (2027 Start)", "Databricks"),
            "excluded")

    def test_intern_title_excluded(self):
        self.assertEqual(
            self.tier("Associate Product Manager Intern", "Coinbase"),
            "excluded")

    def test_europe_locked_role_excluded(self):
        self.assertEqual(
            self.tier("Solutions Engineer, Europe", "Linear"),
            "excluded")

    def test_perm_like_classification_surfaced_as_compliance_lead(self):
        # Compliance-channel notices are the discovery target: surfaced, never excluded.
        self.assertEqual(
            self.tier("Lead Product Manager (Multiple Positions) [REF LPM-B-102-CARC]",
                      "jobs.now", classification="perm-like"),
            "compliance-lead")

    def test_lca_and_recruitment_notice_also_surfaced(self):
        self.assertEqual(
            self.tier("Software Engineer", "jobs.now", classification="lca-like"),
            "compliance-lead")
        self.assertEqual(
            self.tier("Support Specialist", "jobs.now",
                      classification="recruitment-notice-like"),
            "compliance-lead")

    def test_notice_failing_fit_gate_stays_excluded(self):
        # The notice tier changes visibility, not fit: a Spanish-required or
        # out-of-band notice is still excluded for the person.
        self.assertEqual(
            self.tier("Customer Support Specialist - Spanish Required [REF 123]",
                      "jobs.now", classification="perm-like"),
            "excluded")
        self.assertEqual(
            self.tier("Senior Technical Program Manager [REF 456]",
                      "jobs.now", classification="perm-like"),
            "excluded")

    def test_senior_tpm_out_of_band(self):
        self.assertEqual(
            self.tier("Senior Technical Program Manager - Security", "OpenAI"),
            "excluded")

    def test_us_posting_mentioning_europe_in_description_not_excluded(self):
        self.assertNotEqual(
            self.tier("Product Support Specialist",
                      desc="Support our customers across the US and Europe.",
                      loc="San Francisco, CA"),
            "excluded")

    def test_qualified_posting_stays_actionable(self):
        self.assertEqual(
            self.tier("Product Support Specialist",
                      desc="Provide technical support for our SaaS platform via Zendesk.",
                      loc="Remote, United States"),
            "actionable")


class HackerNewsTests(unittest.TestCase):
    def test_company_parsed_from_hiring_title(self):
        with mock.patch.object(hiddenjob, "fetch_json") as fj:
            fj.side_effect = [
                [123, 456],
                {"type": "job", "id": 123, "title": "Stable (YC W20) Is Hiring Product Engineers",
                 "url": "https://example.com/careers", "time": 1790274558, "text": "Join us."},
                {"type": "job", "id": 456, "title": "Acme Hiring Designer",
                 "url": "", "time": 1790274558},
            ]
            records = hiddenjob.hackernews_jobs(limit=2)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["company"], "Stable")
        self.assertEqual(records[0]["source"], "hackernews")
        self.assertTrue(records[0]["url"].startswith("https://example.com"))
        self.assertEqual(records[1]["company"], "Acme")
        # Empty URL falls back to the HN item permalink.
        self.assertIn("news.ycombinator.com/item?id=456", records[1]["url"])

    def test_non_job_items_skipped(self):
        with mock.patch.object(hiddenjob, "fetch_json") as fj:
            fj.side_effect = [
                [123],
                {"type": "story", "id": 123, "title": "Not a job"},
            ]
            records = hiddenjob.hackernews_jobs(limit=1)
        self.assertEqual(records, [])

    def test_api_failure_returns_empty(self):
        with mock.patch.object(hiddenjob, "fetch_json", side_effect=hiddenjob.FetchBlocked("down")):
            self.assertEqual(hiddenjob.hackernews_jobs(limit=5), [])


class FeedSourceTests(unittest.TestCase):
    def test_remoteok_skips_legal_notice(self):
        payload = [
            {"legal": "API Terms of Service: link back to remoteok.com"},
            {"position": "Backend Engineer", "company": "Acme ",
             "url": "https://remoteok.com/remote-jobs/123",
             "date": "2026-09-26T16:00:26+00:00",
             "description": "<p>Build things.</p>"},
            {"slug": "not-a-job"},
        ]
        with mock.patch.object(hiddenjob, "fetch_json", return_value=payload) as fj:
            records = hiddenjob.remoteok_jobs(limit=10)
        fj.assert_called_once()
        self.assertIn("remoteok.com/api", fj.call_args[0][0])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["title"], "Backend Engineer")
        self.assertEqual(records[0]["company"], "Acme")
        self.assertEqual(records[0]["source"], "remoteok")
        self.assertNotIn("<p>", records[0]["description"])

    def test_remoteok_failure_returns_empty(self):
        with mock.patch.object(hiddenjob, "fetch_json", side_effect=hiddenjob.FetchBlocked("down")):
            self.assertEqual(hiddenjob.remoteok_jobs(limit=5), [])

    def test_remotive_records(self):
        payload = {"jobs": [
            {"title": "Content Reviewer", "company_name": "TELUS Digital",
             "url": "https://remotive.com/remote-jobs/x-1",
             "publication_date": "2026-09-21T12:55:11",
             "description": "<p>Review content.</p>"},
            "not-a-dict",
        ]}
        with mock.patch.object(hiddenjob, "fetch_json", return_value=payload):
            records = hiddenjob.remotive_jobs(limit=10)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["company"], "TELUS Digital")
        self.assertEqual(records[0]["date_posted"], "2026-09-21T12:55:11")
        self.assertEqual(records[0]["source"], "remotive")

    def test_arbeitnow_epoch_date(self):
        payload = {"data": [
            {"title": "Designer", "company_name": "Studio X",
             "url": "https://www.arbeitnow.com/jobs/x-1",
             "location": "Berlin", "created_at": 1790553635,
             "description": "Design things."},
        ]}
        with mock.patch.object(hiddenjob, "fetch_json", return_value=payload):
            records = hiddenjob.arbeitnow_jobs(limit=10)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["company"], "Studio X")
        self.assertTrue(records[0]["date_posted"].startswith("2026-"))
        self.assertEqual(records[0]["source"], "arbeitnow")

    def test_usajobs_requires_key(self):
        self.assertEqual(hiddenjob.usajobs_jobs(limit=5, api_key=""), [])

    def test_usajobs_parses_search_result(self):
        body = json.dumps({"SearchResult": {"SearchResultItems": [
            {"MatchedObjectDescriptor": {
                "PositionTitle": "IT Specialist",
                "OrganizationName": "Department of Testing",
                "PositionURI": "https://www.usajobs.gov/job/123",
                "PublicationStartDate": "2026-09-20",
                "JobSummary": "Do IT things."}},
            {"MatchedObjectDescriptor": "not-a-dict"},
            "not-a-dict",
        ]}}).encode()
        resp = mock.MagicMock()
        resp.status = 200
        resp.read.return_value = body
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = resp
        with mock.patch.object(hiddenjob.urllib.request, "urlopen", return_value=ctx) as uo:
            records = hiddenjob.usajobs_jobs(limit=10, api_key="k", keyword="IT")
        called_url = uo.call_args[0][0].full_url
        self.assertIn("data.usajobs.gov/api/search", called_url)
        self.assertIn("Keyword=IT", called_url)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["title"], "IT Specialist")
        self.assertEqual(records[0]["company"], "Department of Testing")
        self.assertEqual(records[0]["source"], "usajobs")


class AtsAdapterTests(unittest.TestCase):
    def test_smartrecruiters_builds_public_url(self):
        payload = {"content": [{
            "name": "Lead Artist", "uuid": "abc-123",
            "company": {"identifier": "NBCUniversal3", "name": "NBCUniversal"},
            "location": {"fullLocation": "New York, NY"},
            "department": {"label": "Design"},
            "releasedDate": "2026-09-26T00:00:04.332Z",
            "refNumber": "REF1"}]}
        with mock.patch.object(hiddenjob, "fetch_json", return_value=payload):
            postings = hiddenjob.ats_board_postings("smartrecruiters", "NBCUniversal3", "NBCUniversal")
        self.assertEqual(len(postings), 1)
        self.assertEqual(postings[0]["url"], "https://jobs.smartrecruiters.com/NBCUniversal3/abc-123")
        self.assertEqual(postings[0]["title"], "Lead Artist")
        self.assertIn("New York, NY", postings[0]["description"])

    def test_workable_widget_listing(self):
        payload = {"jobs": [{
            "title": "Marketing Manager", "url": "https://apply.workable.com/j/ABC123",
            "shortcode": "ABC123", "published_on": "2026-08-26",
            "department": "Growth",
            "locations": [{"city": "", "country": "Serbia"}]}]}
        with mock.patch.object(hiddenjob, "fetch_json", return_value=payload):
            postings = hiddenjob.ats_board_postings("workable", "jobrack", "JobRack")
        self.assertEqual(len(postings), 1)
        self.assertEqual(postings[0]["url"], "https://apply.workable.com/j/ABC123")
        self.assertEqual(postings[0]["date_posted"], "2026-08-26")
        self.assertIn("Serbia", postings[0]["description"])

    def test_personio_xml_feed(self):
        xml = ('<workzag-jobs><position><id>42</id><subcompany>Acme AG</subcompany>'
               '<office>Hybrid - Berlin</office><department>Eng</department>'
               '<name>Backend Engineer</name>'
               '<jobDescriptions><jobDescription><name>About</name>'
               '<value><![CDATA[Build APIs.]]></value></jobDescription></jobDescriptions>'
               '<createdAt>2026-08-19T10:04:22+00:00</createdAt></position></workzag-jobs>')
        with mock.patch.object(hiddenjob, "fetch", return_value=(xml, "https://x.jobs.personio.com/xml")):
            postings = hiddenjob.ats_board_postings("personio", "x", "")
        self.assertEqual(len(postings), 1)
        self.assertEqual(postings[0]["url"], "https://x.jobs.personio.com/job/42")
        self.assertEqual(postings[0]["company"], "Acme AG")
        self.assertIn("Build APIs.", postings[0]["description"])

    def test_personio_bad_xml_blocked(self):
        with mock.patch.object(hiddenjob, "fetch", return_value=("not xml", "u")):
            with self.assertRaises(hiddenjob.FetchBlocked):
                hiddenjob.ats_board_postings("personio", "x", "")

    def test_unsupported_kind_still_refused(self):
        with self.assertRaises(hiddenjob.FetchBlocked):
            hiddenjob.ats_board_postings("notarealats", "x", "")
