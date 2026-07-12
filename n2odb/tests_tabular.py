"""Tests for the tabular data-file parse service and upload endpoint."""
from __future__ import annotations

import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from n2odb.tests_common import AuthedWorkbenchTestCase
from django.urls import reverse

from n2odb.models_upload import UploadedDataFile, UploadSubmission
from n2odb.services import tabular


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


class ParseCsvTests(TestCase):
    def test_headers_and_dtype_inference(self):
        ds = tabular.parse_tabular(
            "csv",
            _csv(
                "site,date,n2o_flux,plots,tillage\n"
                "A,2018-05-01,1.2,3,no-till\n"
                "A,2018-05-02,0.8,3,no-till\n"
                "A,2018-05-03,2.1,4,no-till\n"
            ),
            filename="study.csv",
        )
        self.assertEqual(ds.error, "")
        sheet = ds.sheets[0]
        self.assertEqual(sheet.headers, ["site", "date", "n2o_flux", "plots", "tillage"])
        self.assertEqual(sheet.dtypes, ["string", "date", "float", "int", "string"])
        self.assertEqual(sheet.n_rows, 3)
        self.assertEqual(sheet.header_row, 0)

    def test_detects_header_below_title_rows(self):
        ds = tabular.parse_tabular(
            "csv",
            _csv(
                "Nitrous oxide flux export\n"
                "\n"
                "site,date,n2o_flux\n"
                "A,2018-05-01,1.2\n"
                "A,2018-05-02,0.8\n"
                "A,2018-05-03,2.1\n"
            ),
            filename="study.csv",
        )
        sheet = ds.sheets[0]
        self.assertEqual(sheet.headers, ["site", "date", "n2o_flux"])
        self.assertEqual(sheet.n_rows, 3)

    def test_semicolon_delimiter_sniffed(self):
        ds = tabular.parse_tabular(
            "csv",
            _csv("a;b;c\n1;2;3\n4;5;6\n"),
            filename="euro.csv",
        )
        self.assertEqual(ds.sheets[0].headers, ["a", "b", "c"])
        self.assertEqual(ds.sheets[0].n_rows, 2)

    def test_duplicate_headers_are_disambiguated(self):
        ds = tabular.parse_tabular(
            "csv", _csv("x,x,y\n1,2,3\n4,5,6\n"), filename="d.csv"
        )
        self.assertEqual(ds.sheets[0].headers, ["x", "x_1", "y"])

    @override_settings(WORKBENCH_DATA_MAX_MB=0)
    def test_oversized_file_degrades(self):
        ds = tabular.parse_tabular("csv", _csv("a,b\n1,2\n"), filename="d.csv")
        self.assertNotEqual(ds.error, "")
        self.assertEqual(ds.sheets, [])

    @override_settings(WORKBENCH_DATA_MAX_ROWS=2)
    def test_row_cap_marks_truncated(self):
        ds = tabular.parse_tabular(
            "csv", _csv("a,b\n1,2\n3,4\n5,6\n7,8\n"), filename="d.csv"
        )
        sheet = ds.sheets[0]
        self.assertEqual(sheet.n_rows, 4)
        self.assertEqual(sheet.n_rows_stored, 2)
        self.assertTrue(sheet.truncated)


class ParseXlsxTests(TestCase):
    def _make_xlsx(self) -> bytes:
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Fluxes"
        ws.append(["date", "n2o_flux"])
        ws.append(["2018-05-01", 1.2])
        ws.append(["2018-05-02", 0.8])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_parses_named_sheet(self):
        ds = tabular.parse_tabular("xlsx", self._make_xlsx(), filename="s.xlsx")
        self.assertEqual(ds.error, "")
        sheet = ds.sheets[0]
        self.assertEqual(sheet.name, "Fluxes")
        self.assertEqual(sheet.headers, ["date", "n2o_flux"])
        self.assertEqual(sheet.n_rows, 2)

    def test_native_datetime_cells_are_json_safe_iso_strings(self):
        """Real Excel date cells arrive as datetime objects; they must become ISO
        strings so parsed_content (a plain JSONField) can serialize on save."""
        import json
        from datetime import datetime
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["when", "flux"])
        ws.append([datetime(2023, 4, 17, 10, 0), 21.5])
        buf = io.BytesIO()
        wb.save(buf)

        ds = tabular.parse_tabular("xlsx", buf.getvalue(), filename="s.xlsx")
        self.assertEqual(ds.error, "")
        self.assertEqual(ds.sheets[0].rows[0], ["2023-04-17T10:00:00", "21.5"])
        # The crux: content must be JSON-serializable (this is what 500'd before).
        json.dumps(ds.as_content())


class SniffKindTests(TestCase):
    def test_sniff(self):
        self.assertEqual(tabular.sniff_kind("a.csv", b"a,b,c\n"), "csv")
        self.assertEqual(tabular.sniff_kind("a.xlsx", b"PK\x03\x04zzz"), "xlsx")
        # A zip without an .xlsx name (e.g. a DOCX) is not accepted as data.
        self.assertIsNone(tabular.sniff_kind("a.docx", b"PK\x03\x04zzz"))


class UploadDataEndpointTests(AuthedWorkbenchTestCase):
    def setUp(self):
        super().setUp()
        self.submission = UploadSubmission.objects.create()
        self.token = self.submission.public_token

    def test_upload_csv_stores_parsed_content(self):
        f = SimpleUploadedFile(
            "study.csv",
            b"site,date,n2o_flux\nA,2018-05-01,1.2\nA,2018-05-02,0.8\n",
            content_type="text/csv",
        )
        resp = self.client.post(reverse("workbench-data", args=[self.token]), {"data": f})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "parsed")
        self.assertEqual(data["row_count"], 2)
        self.assertEqual(data["preview"][0]["headers"], ["site", "date", "n2o_flux"])

        self.submission.refresh_from_db()
        self.assertEqual(self.submission.parsed_content["source"], "file")
        self.assertEqual(len(self.submission.parsed_content["sheets"]), 1)
        self.assertTrue(
            UploadedDataFile.objects.filter(submission=self.submission, parse_status="parsed").exists()
        )

    def test_reject_unsupported_file(self):
        f = SimpleUploadedFile("notes.pdf", b"%PDF-1.4 xxx", content_type="application/pdf")
        resp = self.client.post(reverse("workbench-data", args=[self.token]), {"data": f})
        self.assertEqual(resp.status_code, 400)
