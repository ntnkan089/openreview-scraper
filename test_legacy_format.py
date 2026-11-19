import json
import csv
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from export_legacy_format import BASE, convert_row, export, make_headers, make_mapping, validate_files


class LegacyFormatTests(unittest.TestCase):
    def source(self):
        return {
            "paper_id": "paper-a", "link": "https://openreview.net/forum?id=paper-a",
            "title": "Paper A", "authors": "[]", "authorids": "[]", "abstract": "Abstract",
            "submission_number": "62286", "review_count": "1",
            "content.keywords": '["learning", "agents"]', "content.primary_area": "LLMs",
            "Review 1 review_id": "review-a", "Review 1 reviewer_alias": "Reviewer EXQ4",
            "Review 1 reviewer": "Venue/Reviewer_EXQ4", "Review 1 full_review": "Full\nreview",
            "Review 1 content.rating": "2", "Review 1 score.rating": "2",
            "Review 1 content.confidence": "4", "Review 1 content.informativeness": "2",
            "Review 1 content.critical_strengths": "Verbatim strength\nnext line",
            "Review 1 content.new_unknown_field": "Keep unknown fields too",
        }

    def test_legacy_columns_and_every_original_field_preserved(self):
        original = self.source()
        mapping = make_mapping(list(original))
        headers, maximum = make_headers(mapping)
        converted = convert_row(original, mapping, maximum, {})
        self.assertEqual(headers[:11], BASE)
        self.assertEqual(headers[11:19], ["Review 1", "Reviewer 1", "Rebuttal 1",
                                         "Review 1 Soundness", "Review 1 Presentation", "Review 1 Contribution",
                                         "Review 1 Rating", "Review 1 Confidence"])
        for source, destination in mapping.items():
            self.assertEqual(original[source], converted[destination])
        self.assertEqual(converted["Review 1"], "Full\nreview")
        self.assertEqual(converted["Review 1 Rating"], "2")
        self.assertEqual(converted["Review 1 Informativeness"], "2")
        self.assertEqual(converted["Review 1 New Unknown Field"], "Keep unknown fields too")
        self.assertEqual(converted["authors"], "")
        self.assertEqual(converted["keywords"], "learning, agents")

    def test_rebuttals_and_reviewer_comments_are_matched_by_review_id(self):
        source = self.source()
        mapping = make_mapping(list(source))
        notes = [
            {"id": "comment-a", "replyto": "review-a", "signatures": ["Venue/Reviewer_EXQ4"], "content": {"comment": {"value": "Reviewer clarification"}}},
            {"id": "reply-a", "replyto": "comment-a", "signatures": ["Venue/Authors"], "content": {"comment": {"value": "> quoted reviewer\nActual answer"}}},
        ]
        row = convert_row(source, mapping, 1, {"paper-a": notes})
        self.assertEqual(row["Rebuttal 1"], "Actual answer")
        self.assertIn("Reviewer clarification", row["Review 1 Comments"])
        self.assertEqual(json.loads(row["Public Replies JSON"]), notes)

    def test_string_keywords_and_unpaired_public_author_names(self):
        source = self.source()
        source["content.keywords"] = "learning, agents"
        source["authors"] = '["Author A", "Author B"]'
        source["authorids"] = '["~Author_A1"]'
        row = convert_row(source, make_mapping(list(source)), 1, {})
        self.assertEqual(row["keywords"], "learning, agents")
        self.assertIn("Author B", row["authors"])
        self.assertIn("profile?id=~Author_A1", row["authors"])

    def test_review_slots_extend_past_old_five_reviewer_limit(self):
        source = self.source()
        source["Review 7 review_id"] = "review-seven"
        headers, maximum = make_headers(make_mapping(list(source)))
        self.assertEqual(maximum, 7)
        self.assertIn("Review 7 Confidence", headers)
        self.assertIn("Review 7 Overall Recommendation", headers)

    def test_long_quoted_multiline_reviews_survive_both_file_formats(self):
        source = self.source()
        source["Review 1 full_review"] = ('Quoted "text"\r\nSecond line\rBare CR\nUnicode λ and NUL\x00\n' * 5000)
        mapping = make_mapping(list(source))
        headers, maximum = make_headers(mapping)
        row = dict.fromkeys(headers, "")
        row.update(convert_row(source, mapping, maximum, {}))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            pq.write_table(pa.Table.from_pylist([source]), path / "source.parquet")
            pq.write_table(pa.Table.from_pylist([row]), path / "output.parquet")
            with (path / "output.csv").open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
                writer.writerow(row)
            self.assertEqual(validate_files(path / "source.parquet", path / "output.parquet", path / "output.csv", mapping, headers), 1)

    def test_fresh_snapshot_exports_without_manually_created_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            rows = []
            for i in range(5):
                row = self.source()
                row["paper_id"] = f"paper-{i}"
                rows.append(row)
            pq.write_table(pa.Table.from_pylist(rows), path / "papers.parquet")
            (path / "public_replies.jsonl").write_text("", encoding="utf-8")
            report = export(path, path / "compatible")
            self.assertEqual(report["papers"], 5)
            sample = path / "compatible" / "ICLR_2027_Conference_Active_Submissions_papers_sample.csv"
            with sample.open(encoding="utf-8", newline="") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 4)


if __name__ == "__main__":
    unittest.main()
