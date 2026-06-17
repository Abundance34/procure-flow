import io
import zipfile

import pandas as pd

from core.report_service import build_excel_workbook, excel_mime


def test_excel_report_generation_is_xlsx():
    blob = build_excel_workbook({"Summary": pd.DataFrame([{"Total": 1}]), "Detailed Records": pd.DataFrame([{"Ref": "PR-1"}])})
    assert blob[:2] == b"PK"
    assert zipfile.is_zipfile(io.BytesIO(blob))
    assert excel_mime().endswith("spreadsheetml.sheet")
