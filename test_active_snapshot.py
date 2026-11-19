"""Offline coverage and losslessness checks for the Active Submissions export."""
import copy
import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from active_snapshot import API, AccessBlocked, capture, main, review_row, score, public_response

VENUE = "ICLR.cc/2027/Conference"
ACTIVE = VENUE + "/Submission"


def paper(pid, number, reviewed=True):
    review = {
        "id": "review-" + pid, "forum": pid, "replyto": pid,
        "invitations": [VENUE + f"/Submission{number}/-/Official_Review"],
        "signatures": [VENUE + f"/Submission{number}/Reviewer_EXQ4"],
        "cdate": 100, "mdate": 200, "readers": ["everyone"],
        "content": {k: {"value": v} for k, v in {
            "paper_summary": "Summary\nwith second line", "critical_strengths": "Strengths",
            "critical_weaknesses": "Weaknesses", "questions_for_authors": "Questions",
            "presentation": "3: Clear and easy to follow.", "informativeness": "2: Useful.",
            "soundness": "3: No substantive concerns.", "rating": "2: Weak rejection",
            "confidence": "4: Direct experience", "flag_for_ethics_review": "No",
            "new_field_not_in_old_scraper": "Keep this verbatim, too!",
        }.items()},
    }
    return {"id": pid, "number": number, "readers": ["everyone"], "content": {
        "title": {"value": "Paper " + pid}, "venueid": {"value": ACTIVE},
    }, "details": {"replies": [review] if reviewed else []}}


class FakeAPI:
    def __init__(self, papers, end=None, fail_offset=None):
        self.papers = papers
        self.end = end if end is not None else papers
        self.fail_offset = fail_offset
        self.calls = []

    def get(self, endpoint, params):
        self.calls.append((endpoint, dict(params)))
        if endpoint == "/groups":
            return {"groups": [{"content": {"submission_venue_id": {"value": ACTIVE}, "review_name": {"value": "Official_Review"}}}]}
        assert params["content.venueid"] == ACTIVE
        rows = self.papers if params.get("details") else self.end
        offset = params.get("offset", 0)
        if params.get("details") and offset == self.fail_offset:
            raise AccessBlocked("Verification required")
        return {"notes": copy.deepcopy(rows[offset:offset + params["limit"]]), "count": len(rows)}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name)
        self.quiet = redirect_stdout(io.StringIO())
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        self.temp.cleanup()

    def test_full_capture_all_fields_and_zero_review_papers(self):
        folder, m = capture(FakeAPI([paper("a", 62286), paper("b", 62287, False)]), VENUE, self.out, 1)
        self.assertEqual((m["status"], m["papers"], m["reviews"]), ("complete", 2, 1))
        self.assertTrue(m["coverage_verified"])
        self.assertEqual(m["papers_without_public_reviews"], 1)
        with (folder / "reviews.csv").open(encoding="utf-8-sig", newline="") as f:
            row = next(csv.DictReader(f))
        self.assertEqual(row["score.informativeness"], "2")
        self.assertEqual(row["score.confidence"], "4")
        self.assertEqual(row["content.rating"], "2: Weak rejection")
        self.assertIn("Keep this verbatim, too!", row["full_review"])
        self.assertIn("Summary\nwith second line", row["content.paper_summary"])
        self.assertEqual(row["mdate"], "200")
        self.assertEqual(len(json.loads((folder / "paper_ids.json").read_text())), 2)
        self.assertEqual(len(list((folder / "pages").glob("*.json"))), 2)

    def test_membership_change_never_marked_complete(self):
        _, m = capture(FakeAPI([paper("a", 1)], end=[paper("b", 2)]), VENUE, self.out)
        self.assertEqual(m["status"], "membership_changed")
        self.assertFalse(m["coverage_verified"])
        self.assertEqual(m["missing_ids_at_end"], ["b"])

    def test_resume_keeps_previously_captured_review(self):
        with self.assertRaises(AccessBlocked):
            capture(FakeAPI([paper("a", 1), paper("b", 2)], fail_offset=1), VENUE, self.out, 1)
        folder = next(self.out.iterdir())
        original = (folder / "pages/000000000.json").read_bytes()
        changed = paper("a", 1)
        changed["details"]["replies"][0]["content"]["rating"]["value"] = "8: Accept"
        _, m = capture(FakeAPI([changed, paper("b", 2)]), VENUE, self.out, 1, resume=folder)
        self.assertEqual(m["status"], "complete")
        self.assertEqual((folder / "pages/000000000.json").read_bytes(), original)

    def test_duplicate_ids_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Duplicate submission"):
            capture(FakeAPI([paper("a", 1), paper("a", 1)]), VENUE, self.out, 1)

    def test_missing_replies_rejected(self):
        p = paper("a", 1)
        p.pop("details")
        with self.assertRaisesRegex(RuntimeError, "Replies missing"):
            capture(FakeAPI([p]), VENUE, self.out)

    def test_sample_never_claims_full_coverage(self):
        _, m = capture(FakeAPI([paper("a", 1), paper("b", 2)]), VENUE, self.out, limit=1)
        self.assertEqual(m["status"], "sample")
        self.assertFalse(m["coverage_verified"])

    def test_new_runs_preserve_old_snapshots(self):
        a, _ = capture(FakeAPI([paper("a", 1)]), VENUE, self.out)
        b, _ = capture(FakeAPI([paper("a", 1)]), VENUE, self.out)
        self.assertNotEqual(a, b)
        self.assertTrue((a / "reviews.csv").exists())

    def test_score_does_not_extract_numbers_from_prose(self):
        self.assertIsNone(score("Experiments on 3 models"))
        self.assertIsNone(score(True))
        self.assertEqual(score("4: Confident\nmore detail"), 4)

    def test_authenticated_response_excludes_private_notes_fields_and_true_authors(self):
        p = paper("a", 1)
        p["content"]["authors"] = {"value": ["Private Author"], "readers": [VENUE + "/Authors"]}
        p["tauthor"] = "private@example.org"
        private = copy.deepcopy(p["details"]["replies"][0])
        private["readers"] = [VENUE + "/Program_Chairs"]
        p["details"]["replies"].append(private)
        cleaned = public_response({"notes": [p], "count": 1})["notes"][0]
        self.assertNotIn("authors", cleaned["content"])
        self.assertNotIn("tauthor", cleaned)
        self.assertEqual(len(cleaned["details"]["replies"]), 1)
        self.assertIn("authors", p["content"])

    def test_rate_limited_final_check_preserves_completed_exports(self):
        class LimitedAPI(FakeAPI):
            def get(self, endpoint, params):
                if endpoint == "/notes" and not params.get("details"):
                    raise RuntimeError("429 Too Many Requests")
                return super().get(endpoint, params)
        with self.assertRaisesRegex(RuntimeError, "429"):
            capture(LimitedAPI([paper("a", 1)]), VENUE, self.out)
        folder = next(self.out.iterdir())
        m = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(m["status"], "exported_verification_pending")
        self.assertEqual((m["papers"], m["reviews"]), (1, 1))
        self.assertTrue((folder / "reviews.csv").exists())
        self.assertFalse(m["coverage_verified"])

    def test_challenge_is_not_treated_as_empty_success(self):
        api = API(delay=0)
        class Response:
            status_code = 403
            def json(self):
                return {"name": "ChallengeRequiredError", "message": "Verification required", "details": {"challengeUrl": "https://openreview.net/challenge"}}
        with patch.object(api.session, "get", return_value=Response()):
            with self.assertRaises(AccessBlocked):
                api.get("/notes", {})

    def test_one_command_exports_compatible_csv_parquet_and_public_rebuttal(self):
        import pyarrow.parquet as pq
        from export_legacy_format import BASE, OLD_SCORES

        submission = paper("a", 1)
        submission["details"]["replies"].append({
            "id": "reply-a", "forum": "a", "replyto": "review-a",
            "invitations": [VENUE + "/Submission1/-/Official_Comment"],
            "signatures": [VENUE + "/Submission1/Authors"], "readers": ["everyone"],
            "content": {"comment": {"value": 'Author answer with "quotes"\nand Unicode λ.'}},
        })
        api = FakeAPI([submission, paper("b", 2, False)])
        argv = ["active_snapshot.py", "--compatible", "--out", str(self.out)]
        with patch("sys.argv", argv), patch("active_snapshot.API", return_value=api):
            self.assertEqual(main(), 0)
        folder = next(self.out.iterdir())
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertTrue(manifest["coverage_verified"])
        csv_path = folder / manifest["compatible_exports"]["csv"]
        parquet_path = folder / manifest["compatible_exports"]["parquet"]
        with csv_path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            headers = reader.fieldnames
            rows = list(reader)
        self.assertEqual(rows, pq.read_table(parquet_path).to_pylist())
        old_headers = list(BASE)
        for i in range(1, 6):
            old_headers += [f"Review {i}", f"Reviewer {i}", f"Rebuttal {i}"]
            old_headers += [f"Review {i} {field}" for field in OLD_SCORES]
        self.assertEqual(headers[:len(old_headers)], old_headers)
        self.assertEqual(rows[0]["Review 1 Rating"], "2: Weak rejection")
        self.assertEqual(rows[0]["Review 1 Confidence Numeric"], "4")
        self.assertEqual(rows[0]["Review 1 Paper Summary"], "Summary\nwith second line")
        self.assertEqual(rows[0]["Review 1 New Field Not In Old Scraper"], "Keep this verbatim, too!")
        self.assertEqual(rows[0]["Rebuttal 1"], 'Author answer with "quotes"\nand Unicode λ.')
        self.assertEqual(rows[1]["review_count"], "0")
        self.assertEqual(json.loads((folder / "validation.json").read_text())["status"], "passed")

    def test_compatible_exports_survive_rate_limit_and_can_resume(self):
        class LimitedAPI(FakeAPI):
            def get(self, endpoint, params):
                if endpoint == "/notes" and not params.get("details"):
                    raise RuntimeError("429 Too Many Requests")
                return super().get(endpoint, params)

        argv = ["active_snapshot.py", "--compatible", "--out", str(self.out)]
        with patch("sys.argv", argv), patch("active_snapshot.API", return_value=LimitedAPI([paper("a", 1)])):
            self.assertEqual(main(), 1)
        folder = next(self.out.iterdir())
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "exported_verification_pending")
        self.assertFalse(manifest["coverage_verified"])
        paths = [folder / manifest["compatible_exports"][key] for key in ("csv", "parquet")]
        original = [path.read_bytes() for path in paths]
        _, resumed = capture(FakeAPI([paper("a", 1)]), VENUE, self.out, resume=folder, compatible=True)
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual([path.read_bytes() for path in paths], original)

    def test_export_failure_is_not_reported_as_success(self):
        argv = ["active_snapshot.py", "--compatible", "--out", str(self.out)]
        with patch("sys.argv", argv), patch("active_snapshot.API", return_value=FakeAPI([paper("a", 1)])), \
                patch("active_snapshot.export_compatible", side_effect=RuntimeError("conversion failed")):
            self.assertEqual(main(), 1)
        manifest = json.loads((next(self.out.iterdir()) / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "interrupted")
        self.assertEqual(manifest["phase"], "exporting_compatible")
        self.assertFalse(manifest["coverage_verified"])

    def test_blocked_download_does_not_create_compatible_exports(self):
        argv = ["active_snapshot.py", "--compatible", "--out", str(self.out)]
        with patch("sys.argv", argv), \
                patch("active_snapshot.API", return_value=FakeAPI([paper("a", 1)], fail_offset=0)):
            self.assertEqual(main(), 1)
        folder = next(self.out.iterdir())
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "blocked")
        self.assertFalse((folder / "compatible").exists())


if __name__ == "__main__":
    unittest.main()
