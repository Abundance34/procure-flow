"""Excel report generation helpers."""
from __future__ import annotations

from io import BytesIO
from typing import Mapping

import pandas as pd


def _safe_sheet_name(name: str) -> str:
    cleaned = "".join(ch for ch in str(name) if ch not in r"[]:*?/\\")[:31]
    return cleaned or "Sheet"


def build_excel_workbook(sheets: Mapping[str, pd.DataFrame], title: str = "ProcureFlow Report") -> bytes:
    """Return an .xlsx workbook containing one or more sanitized sheets."""
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        used: set[str] = set()
        for raw_name, df in sheets.items():
            name = _safe_sheet_name(raw_name)
            base = name
            i = 2
            while name in used:
                suffix = f"_{i}"
                name = (base[: 31 - len(suffix)] + suffix)[:31]
                i += 1
            used.add(name)
            data = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame(df)
            data.to_excel(writer, index=False, sheet_name=name)
            ws = writer.book[name]
            ws.freeze_panes = "A2"
            for cell in ws[1]:
                cell.style = "Headline 4"
            for col in ws.columns:
                values = [str(c.value or "") for c in col[:200]]
                width = min(max(len(v) for v in values) + 2, 42)
                ws.column_dimensions[col[0].column_letter].width = width
    return output.getvalue()


def excel_mime() -> str:
    return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
