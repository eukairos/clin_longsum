# clin_longsum - tiered longitudinal clinical summarization
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only

"""
Offline tests for src/ingest - no Postgres, no LLM, synthetic text only (no patient
data). LONGSUM_DATA_ROOT is pointed at a scratch folder under this directory BEFORE
config is imported, so nothing touches a real cohort.

    cd C:~/clin_longsum/src
    python ingest/tests/test_ingest_offline.py
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = HERE / "_tmp"
if TMP.exists():
    shutil.rmtree(TMP)
(TMP / "data" / "interim").mkdir(parents=True)
(TMP / "drop").mkdir()
os.environ["LONGSUM_DATA_ROOT"] = str(TMP)
os.environ.pop("LONGSUM_NAMESPACE", None)
sys.path.insert(0, str(HERE.parent))            # src/ingest
sys.path.insert(0, str(HERE.parents[1]))        # src

import pandas as pd                              # noqa: E402

import config                                    # noqa: E402
import a2_build_cohort as a2                     # noqa: E402
import ingest_derive as dv                       # noqa: E402
import ingest_project as pj                      # noqa: E402
import ingest_source as so                       # noqa: E402
import ingest_surface as sf                      # noqa: E402

assert config.DATA_ROOT == TMP.resolve(), "scratch data root not in effect"


def _text(tag: str, n: int = 12) -> str:
    """Synthetic BHC: n distinct sentences, >50 words, unique per tag."""
    return " ".join(f"The {tag} finding number {i} was reviewed by the team on the ward today."
                    for i in range(n))


def _rec(sid, hadm, admit, text, expired=False, signed="2026-09-20T09:00:00"):
    return so.EHRDischarge(sid, hadm, pd.Timestamp(admit).to_pydatetime(),
                           pd.Timestamp(admit).to_pydatetime(), pd.Timestamp(signed).to_pydatetime(),
                           "MED", "EW EMER.", "HOME", expired, text)


class TestSource(unittest.TestCase):
    def test_csv_drop(self):
        rows = pd.DataFrame([
            {"subject_id": "1", "hadm_id": "11", "admittime": "2026-09-01T08:00:00",
             "dischtime": "2026-09-05T10:00:00", "signed_at": "2026-09-06T09:00:00",
             "bhc_text": "old text", "hospital_expire_flag": "0"},
            {"subject_id": "1", "hadm_id": "11", "admittime": "2026-09-01T08:00:00",
             "dischtime": "2026-09-05T10:00:00", "signed_at": "2026-09-07T09:00:00",
             "bhc_text": "newer text", "hospital_expire_flag": "0"},
            {"subject_id": "2", "hadm_id": "21", "admittime": "5/2/2026 0:14",
             "dischtime": "", "signed_at": "2026-09-06T09:00:00", "bhc_text": "x",
             "hospital_expire_flag": "0"},
            {"subject_id": "3", "hadm_id": "31", "admittime": "2026-09-01",
             "dischtime": "", "signed_at": "2026-09-06T09:00:00", "bhc_text": "  ",
             "hospital_expire_flag": "0"},
            {"subject_id": "4", "hadm_id": "41", "admittime": "2026-08-01",
             "dischtime": "", "signed_at": "2026-08-02T09:00:00", "bhc_text": "before watermark",
             "hospital_expire_flag": "1"},
        ])
        rows.to_csv(TMP / "drop" / "batch.csv", index=False)
        recs, rejects = so.CsvDropSource(TMP / "drop").fetch(datetime(2026, 8, 31))
        self.assertEqual([(r.subject_id, r.hadm_id) for r in recs], [(1, 11)])
        self.assertEqual(recs[0].bhc_text, "newer text")                  # latest signed wins
        reasons = " | ".join(r["reason"] for r in rejects)
        self.assertIn("not ISO 8601", reasons)                            # day-first refused
        self.assertIn("empty bhc_text", reasons)
        self.assertIn("superseded", reasons)


class TestDerive(unittest.TestCase):
    def test_new_patient(self):
        acts = dv.plan_subject([_rec(1, 12, "2026-09-10", _text("b")),
                                _rec(1, 11, "2026-09-01", _text("a"))], [])
        self.assertEqual([(a.kind, a.rec.hadm_id, a.row["admit_seq"]) for a in acts],
                         [("insert", 11, 1), ("insert", 12, 2)])
        self.assertTrue(acts[0].first_admission)
        self.assertFalse(acts[1].first_admission)
        self.assertIsNone(acts[0].row["dedup_jaccard_to_prev"])
        self.assertTrue(all(a.row["is_holdout"] for a in acts))

    def test_existing_patient_matches_batch_a2(self):
        t1, t2 = _text("a"), _text("a", 6) + " " + _text("c", 8)          # t2 repeats half of t1
        ex = [dv.Existing(11, 1, date(2026, 9, 1), t1, False, True)]
        acts = dv.plan_subject([_rec(1, 12, "2026-09-10", t2)], ex)
        self.assertEqual((acts[0].kind, acts[0].row["admit_seq"]), ("insert", 2))
        batch = a2.dedup_patient_bhc([{"hadm_id": 11, "bhc_text": t1},
                                      {"hadm_id": 12, "bhc_text": t2}])[1]
        self.assertEqual(acts[0].row["bhc_delta"], batch["bhc_delta"])
        self.assertEqual(acts[0].row["dedup_jaccard_to_prev"], batch["dedup_jaccard_to_prev"])
        self.assertNotIn("The a finding", acts[0].row["bhc_delta"])      # repeated half removed
        self.assertIn("The c finding", acts[0].row["bhc_delta"])

    def test_duplicate_amendment_out_of_order_after_death(self):
        t = _text("a")
        ex = [dv.Existing(11, 1, date(2026, 9, 1), t, False, True),
              dv.Existing(12, 2, date(2026, 9, 10), _text("b"), False, False)]
        acts = {a.rec.hadm_id: a for a in dv.plan_subject([
            _rec(1, 11, "2026-09-01", t),
            _rec(1, 12, "2026-09-10", _text("b") + " Addendum."),
            _rec(1, 13, "2026-09-05", _text("c")),
            _rec(1, 14, "2026-09-10", _text("d")),                          # same day: allowed
        ], ex)}
        self.assertEqual(acts[11].kind, "duplicate")
        self.assertEqual(acts[12].kind, "amendment")
        self.assertFalse(acts[12].detail["admission_folded_in"])
        self.assertEqual(acts[13].kind, "out_of_order")
        self.assertEqual((acts[14].kind, acts[14].row["admit_seq"]), ("insert", 3))
        dead = dv.plan_subject([_rec(2, 22, "2026-09-12", _text("e"))],
                               [dv.Existing(21, 1, date(2026, 9, 1), _text("f"), True, True)])
        self.assertEqual(dead[0].kind, "after_death")


class TestProject(unittest.TestCase):
    def setUp(self):
        self.path = config.INTERIM_DIR / "cohort_admissions.csv"
        hdr = ",".join(pj.ADMISSION_COLS)
        body = ("1,10,1,5/2/2175 0:14,13/2/2175 16:40,MED,EW,HOME,0,FALSE,\"a, b\nc\",d,\r\n"
                "2,10,2,15/6/2175 16:18,23/6/2175 16:40,MED,EW,HOME,0,TRUE,t,d,0.5\r\n")
        self.path.write_bytes((hdr + "\r\n" + body).encode("utf-8"))
        self.original = self.path.read_bytes()

    def _row(self, hadm, seq, when):
        return {"hadm_id": hadm, "subject_id": 10, "admit_seq": seq,
                "admittime": when, "dischtime": when, "service": "MED",
                "admission_type": "EW", "discharge_location": "HOME",
                "hospital_expire_flag": False, "is_holdout": True,
                "bhc_text": "line1\nline2, with comma", "bhc_delta": "delta",
                "dedup_jaccard_to_prev": 0.25}

    def test_append_preserves_bytes_and_parses(self):
        pj.assert_iso_parse_safe()
        n = pj.append_rows(self.path, [self._row(3, 3, datetime(2175, 7, 5, 10, 30))],
                           pj.ADMISSION_COLS)
        self.assertEqual(n, 1)
        new = self.path.read_bytes()
        self.assertTrue(new.startswith(self.original))                   # existing bytes untouched
        self.assertIn(b"\r\n3,10,3,2175-07-05 10:30:00", new)
        self.assertIn(b",TRUE,", new[len(self.original):])               # file's casing mirrored
        self.assertEqual(pj.append_rows(self.path, [self._row(3, 3, datetime(2175, 7, 5))],
                                        pj.ADMISSION_COLS), 0)           # idempotent

        import cohort
        import holdout
        df = cohort.load()
        r = df[df["hadm_id"] == 3].iloc[0]
        self.assertEqual(r["admittime"], pd.Timestamp("2175-07-05 10:30:00"))   # not 7 May
        self.assertEqual(df[df["hadm_id"] == 1].iloc[0]["admittime"], pd.Timestamp("2175-02-05 00:14"))
        self.assertEqual(holdout.holdout_hadms(), {2, 3})
        self.assertEqual(df[df["hadm_id"] == 3].iloc[0]["bhc_text"], "line1\nline2, with comma")

    def test_refresh_patients(self):
        p = config.INTERIM_DIR / "cohort_patients.csv"
        p.write_text('subject_id,n_admissions,conditions_json\n10,2,"[1, 2]"\n11,4,[]\n',
                     encoding="utf-8")
        self.assertEqual(pj.refresh_patients(p, {10: 3, 12: 1}), (1, 1))
        got = pd.read_csv(p)
        self.assertEqual(got.set_index("subject_id")["n_admissions"].to_dict(), {10: 3, 11: 4, 12: 1})
        self.assertEqual(got.set_index("subject_id").loc[10, "conditions_json"], "[1, 2]")


class TestSurface(unittest.TestCase):
    def test_queue_and_hide(self):
        df = pd.DataFrame({
            "subject_id": [10, 10, 10, 10, 20],
            "hadm_id": [1, 2, 3, 4, 5],
            "admit_seq": [1, 2, 3, 4, 1],
            "admittime": pd.to_datetime(["2175-01-01", "2175-02-01", "2175-03-01",
                                         "2175-04-01", "2175-05-01"]),
            "service": ["MED"] * 5, "bhc_text": ["t"] * 5,
            "is_holdout": ["False", "False", "True", "True", "True"],
        })
        st = sf.queue_state(df)
        self.assertEqual(st[10]["oldest"]["hadm_id"], 3)
        self.assertEqual(st[10]["queued"], [4])
        self.assertEqual(st[20]["n_established"], 0)
        rows = [{"subject_id": 10, "hadm_id": 4, "summary_md": "newer"},
                {"subject_id": 99, "hadm_id": 9, "summary_md": "other"}]
        keep, hidden, need = sf.plan_incoming_rows(rows, st)
        self.assertEqual(hidden, [(10, 4)])
        self.assertEqual([r["hadm_id"] for r in keep], [9])
        self.assertEqual([r["hadm_id"] for r in need], [3])                # first-admission pt 20 excluded

    def test_review_reset_decision(self):
        f = sf.review_reset_decision
        ts = datetime(2026, 9, 1)
        self.assertEqual(f({"incoming_hadm_id": 3, "review_status": "approved",
                            "folded_in_at": ts}, 3), "reset_with_archive")
        self.assertEqual(f({"incoming_hadm_id": 3, "review_status": "approved",
                            "folded_in_at": None}, 3), "awaiting_foldin")
        self.assertEqual(f({"incoming_hadm_id": 3, "review_status": "pending_review",
                            "folded_in_at": ts}, 3), "clear_folded_flag")
        self.assertEqual(f({"incoming_hadm_id": 3, "review_status": None,
                            "folded_in_at": None}, 3), "none")
        self.assertEqual(f({"incoming_hadm_id": 4, "review_status": "approved",
                            "folded_in_at": ts}, 3), "none")
        self.assertEqual(f(None, 3), "none")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2, exit=False)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
