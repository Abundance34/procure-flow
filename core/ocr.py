from __future__ import annotations

import hashlib
import json
import re
import shutil
from io import BytesIO
from typing import Any

try:
    from PIL import Image, ImageOps, ImageEnhance, ImageFilter
except Exception:  # pragma: no cover
    Image = None
    ImageOps = None
    ImageEnhance = None
    ImageFilter = None

try:
    import pytesseract
except Exception:  # pragma: no cover
    pytesseract = None

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None

from core.db import df_query


def file_hash(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


def tesseract_available() -> bool:
    if pytesseract is None:
        return False
    try:
        cmd = getattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract")
        return bool(shutil.which(cmd) or shutil.which("tesseract"))
    except Exception:
        return False


def _normalise_spaces(text: str) -> str:
    text = text.replace("\x0c", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def preprocess_image_variants(image):
    """Return multiple OCR-friendly variants for difficult receipts.

    Receipts often fail because photos are low-contrast, small, rotated, or faded.
    We try grayscale, high contrast, threshold, sharpened, and resized versions and
    keep the longest/confident-looking OCR output.
    """
    if ImageOps is None or ImageEnhance is None:
        return [image]
    image = ImageOps.exif_transpose(image)
    variants = []
    base = image.convert("RGB")
    gray = base.convert("L")
    width, height = gray.size
    scale = 3 if max(width, height) < 1200 else 2 if max(width, height) < 1800 else 1
    if scale > 1:
        gray = gray.resize((width * scale, height * scale))
    variants.append(gray)
    variants.append(ImageEnhance.Contrast(gray).enhance(2.2))
    variants.append(ImageEnhance.Sharpness(ImageEnhance.Contrast(gray).enhance(2.0)).enhance(1.8))
    if ImageFilter is not None:
        variants.append(gray.filter(ImageFilter.SHARPEN))
    threshold = ImageEnhance.Contrast(gray).enhance(2.5).point(lambda p: 255 if p > 160 else 0)
    variants.append(threshold)
    return variants


def _score_text(text: str) -> tuple[int, int]:
    words = len(re.findall(r"[A-Za-z0-9]{2,}", text or ""))
    amounts = len(re.findall(r"(?:₦|NGN|N)?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?", text or ""))
    return words + amounts * 3, len(text or "")


def _run_tesseract(image, config: str = "") -> str:
    if pytesseract is None:
        raise RuntimeError("pytesseract is not installed. Install pytesseract and the Tesseract OCR engine.")
    if not tesseract_available():
        raise RuntimeError(
            "Tesseract OCR engine is not installed or not in PATH. Install Tesseract for Windows, then restart CMD. "
            "Digital PDFs can still be read by PyMuPDF, but scanned receipts/images need Tesseract."
        )
    return pytesseract.image_to_string(image, config=config or "--oem 3 --psm 6")


def _ocr_image_bytes(image_bytes: bytes) -> tuple[str, dict[str, Any]]:
    if Image is None:
        raise RuntimeError("Pillow is not installed.")
    image = Image.open(BytesIO(image_bytes))
    attempts = []
    errors = []
    for idx, variant in enumerate(preprocess_image_variants(image)):
        for config in ("--oem 3 --psm 6", "--oem 3 --psm 4", "--oem 3 --psm 11"):
            try:
                text = _run_tesseract(variant, config=config)
                attempts.append({"variant": idx, "config": config, "text": text, "score": _score_text(text)})
            except Exception as exc:
                errors.append(str(exc))
                break
    if not attempts:
        raise RuntimeError(errors[-1] if errors else "OCR failed with no output.")
    best = sorted(attempts, key=lambda x: x["score"], reverse=True)[0]
    return _normalise_spaces(best["text"]), {"engine": "tesseract", "attempts": len(attempts), "best_variant": best["variant"], "best_config": best["config"]}


def _extract_pdf_text_direct(pdf_bytes: bytes, max_pages: int = 8) -> str:
    if fitz is None:
        return ""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    texts = []
    for page_index in range(min(len(doc), max_pages)):
        try:
            texts.append(doc[page_index].get_text("text") or "")
        except Exception:
            pass
    return _normalise_spaces("\n".join(texts))


def _ocr_pdf_bytes(pdf_bytes: bytes, max_pages: int = 8) -> tuple[str, dict[str, Any]]:
    if fitz is None:
        raise RuntimeError("PDF reading requires PyMuPDF. Install it with: pip install pymupdf")
    direct_text = _extract_pdf_text_direct(pdf_bytes, max_pages=max_pages)
    # Digital PDFs should not be rasterized when text is extractable.
    if len(direct_text) >= 30 and len(re.findall(r"[A-Za-z0-9]{2,}", direct_text)) >= 5:
        return direct_text, {"engine": "pymupdf-direct", "pages": min(max_pages, 8), "ocr_used": False}
    if Image is None:
        raise RuntimeError("Pillow is not installed.")
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    meta = {"engine": "tesseract-pdf", "pages": min(len(doc), max_pages), "ocr_used": True}
    for page_index in range(min(len(doc), max_pages)):
        page = doc[page_index]
        # Higher DPI helps small receipt fonts.
        pix = page.get_pixmap(dpi=260)
        image = Image.open(BytesIO(pix.tobytes("png")))
        page_text, _ = _ocr_image_bytes(_image_to_png_bytes(image))
        pages.append(page_text)
    return _normalise_spaces("\n\n".join(pages)), meta


def _image_to_png_bytes(image) -> bytes:
    bio = BytesIO()
    image.save(bio, format="PNG")
    return bio.getvalue()


def extract_text(uploaded_file) -> tuple[str, dict[str, Any], str | None]:
    if uploaded_file is None:
        return "", {}, "No file uploaded."
    name = uploaded_file.name.lower()
    data = uploaded_file.getvalue()
    meta = {"file_name": uploaded_file.name, "file_hash": file_hash(data), "file_size": len(data), "ocr_engine": "auto"}
    try:
        if name.endswith(".pdf"):
            text, engine_meta = _ocr_pdf_bytes(data)
        else:
            text, engine_meta = _ocr_image_bytes(data)
        meta.update(engine_meta)
        meta["extracted_chars"] = len(text or "")
        return text.strip(), meta, None if text.strip() else "OCR completed but no text was detected. Try a clearer scan/photo."
    except Exception as exc:
        meta["extracted_chars"] = 0
        return "", meta, str(exc)


def _first_match(patterns: list[str], text: str, flags=re.IGNORECASE):
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return match.group(1).strip()
    return ""


def _amount_from(value: str) -> float:
    value = str(value or "").replace(",", "").replace("₦", "").replace("NGN", "").strip()
    # Avoid stripping N from words; remove leading currency only.
    value = re.sub(r"^[Nn]\s*", "", value)
    try:
        return float(value)
    except Exception:
        return 0.0


def infer_document_type(text: str) -> str:
    lower = (text or "").lower()
    if any(x in lower for x in ["receipt", "paid", "payment received", "pos", "rrn", "approval code", "cash received"]):
        return "Receipt"
    if any(x in lower for x in ["invoice", "amount due", "balance due", "due date", "payment terms"]):
        return "Invoice"
    return "Invoice/Receipt"


def infer_payment_method(text: str) -> str:
    lower = (text or "").lower()
    if any(x in lower for x in ["pos", "terminal", "rrn", "stan", "merchant id"]):
        return "POS/Card"
    if any(x in lower for x in ["card", "visa", "mastercard", "verve", "auth code"]):
        return "Card"
    if any(x in lower for x in ["transfer", "bank", "account", "txn", "transaction reference", "session id"]):
        return "Bank Transfer"
    if any(x in lower for x in ["cheque", "check no"]):
        return "Cheque"
    if any(x in lower for x in ["wallet", "opay", "moniepoint", "paga", "mobile money"]):
        return "Mobile Money"
    if any(x in lower for x in ["cash", "cashier", "change"]):
        return "Cash"
    return "Bank Transfer"


def parse_ocr_text(text: str, vendors_df=None) -> dict[str, Any]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    joined = "\n".join(lines)
    lower = joined.lower()
    result = {"raw_text": text, "fields": {}, "line_items": [], "bank_details": {}, "receipt_details": {}, "confidence": {}, "warnings": []}

    vendor_guess = ""
    skip_words = {"invoice", "receipt", "tax invoice", "payment receipt", "pos receipt"}
    for line in lines[:12]:
        clean = re.sub(r"[^A-Za-z0-9 &.,'-]", "", line).strip()
        if len(clean) >= 3 and clean.lower() not in skip_words and not re.fullmatch(r"[\d\s,.:/#\-|]+", clean):
            vendor_guess = clean[:120]
            break

    matched_vendor_id = None
    matched_vendor_name = ""
    if vendors_df is not None and not vendors_df.empty:
        for _, row in vendors_df.iterrows():
            name = str(row.get("name", ""))
            if name and name.lower() in lower:
                matched_vendor_id = int(row["id"])
                matched_vendor_name = name
                break

    invoice_no = _first_match([
        r"(?:invoice\s*(?:no|number|#)|inv\s*(?:no|#))\s*[:#-]?\s*([A-Za-z0-9\-/]+)",
        r"(?:bill\s*(?:no|number|#))\s*[:#-]?\s*([A-Za-z0-9\-/]+)",
    ], joined)
    receipt_no = _first_match([
        r"(?:receipt\s*(?:no|number|#)|rcpt\s*(?:no|#))\s*[:#-]?\s*([A-Za-z0-9\-/]+)",
        r"(?:transaction\s*(?:id|ref|reference)|txn\s*(?:id|ref)|rrn|stan)\s*[:#-]?\s*([A-Za-z0-9\-/]+)",
    ], joined)
    date_value = _first_match([
        r"(?:date|paid on|payment date|transaction date|invoice date|receipt date)\s*[:#-]?\s*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})",
        r"(?:date|paid on|payment date|transaction date|invoice date|receipt date)\s*[:#-]?\s*(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})",
        r"(?:date|paid on|payment date|transaction date|invoice date|receipt date)\s*[:#-]?\s*(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{2,4})",
        r"\b(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})\b",
        r"\b(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\b",
        r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{2,4})\b",
    ], joined)
    due_date = _first_match([r"(?:due\s*date)\D{0,20}(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{2,4})"], joined)

    amount_candidates = []
    for pattern in [
        r"(?:grand\s*total|total\s*amount|amount\s*due|balance\s*due|total|amount paid)\D{0,25}(?:₦|NGN|N)?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)",
        r"(?:₦|NGN|N)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)",
    ]:
        for match in re.finditer(pattern, joined, flags=re.IGNORECASE):
            value = _amount_from(match.group(1))
            if value > 0:
                amount_candidates.append(value)
    if not amount_candidates:
        for raw in re.findall(r"\b[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{2})?\b|\b[0-9]+\.[0-9]{2}\b", joined):
            value = _amount_from(raw)
            if value >= 50:
                amount_candidates.append(value)
    total_amount = max(amount_candidates) if amount_candidates else 0.0
    tax_raw = _first_match([r"(?:vat|tax)\D{0,20}(?:₦|NGN|N)?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)"], joined)
    tax_amount = _amount_from(tax_raw) if tax_raw else 0.0

    bank_name = _first_match([r"(?:bank)\s*[:#-]?\s*([A-Za-z ]{3,40})"], joined)
    account_no = _first_match([r"(?:account\s*(?:no|number)|acct\s*(?:no|number))\s*[:#-]?\s*([0-9]{8,12})"], joined)
    transfer_ref = _first_match([r"(?:transaction\s*(?:id|ref|reference)|session\s*id|payment\s*ref)\s*[:#-]?\s*([A-Za-z0-9\-/]+)"], joined)
    rrn = _first_match([r"(?:rrn|retrieval\s*reference)\s*[:#-]?\s*([A-Za-z0-9\-/]+)"], joined)
    auth_code = _first_match([r"(?:auth(?:orization)?\s*code|approval\s*code)\s*[:#-]?\s*([A-Za-z0-9\-/]+)"], joined)
    terminal_id = _first_match([r"(?:terminal\s*id|tid)\s*[:#-]?\s*([A-Za-z0-9\-/]+)"], joined)

    category = "Other"
    category_keywords = {
        "Diesel/Fuel": ["diesel", "fuel", "petrol", "gasoline", "generator"],
        "Office Supplies": ["paper", "pen", "office", "stationery", "printer", "toner"],
        "Repairs/Maintenance": ["repair", "maintenance", "spare", "service", "mechanic", "fix"],
        "Transport/Logistics": ["transport", "delivery", "logistics", "fare", "dispatch", "courier"],
        "Staff Welfare": ["food", "meal", "water", "welfare", "refreshment", "lunch"],
        "ICT/Software": ["software", "license", "computer", "laptop", "keyboard", "mouse", "internet"],
        "Utilities": ["electricity", "utility", "water bill", "power", "subscription"],
        "Construction Materials": ["cement", "sand", "block", "steel", "rod", "paint", "wood"],
        "Professional Services": ["consulting", "legal", "audit", "professional", "service fee"],
    }
    for cat, words in category_keywords.items():
        if any(word in lower for word in words):
            category = cat
            break

    # Conservative line item guess: keeps user editable, never auto-final.
    for line in lines:
        if any(x in line.lower() for x in ["total", "subtotal", "tax", "vat", "balance"]):
            continue
        match = re.search(r"(.+?)\s+([0-9]+(?:\.[0-9]+)?)?\s*(?:x)?\s*(?:₦|NGN|N)?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})|[0-9]+\.[0-9]{2})$", line, flags=re.IGNORECASE)
        if match:
            item_name = match.group(1).strip(" -:\t")
            total = _amount_from(match.group(3))
            qty = float(match.group(2) or 1)
            if item_name and total > 0 and 3 <= len(item_name) < 100:
                result["line_items"].append({"item_name": item_name, "quantity": qty, "unit_price": total / max(qty, 1), "total": total, "confidence": 0.45})

    doc_type = infer_document_type(joined)
    pay_method = infer_payment_method(joined)
    result["fields"] = {
        "document_type": doc_type,
        "payment_method": pay_method,
        "vendor_guess": vendor_guess,
        "matched_vendor_id": matched_vendor_id,
        "matched_vendor_name": matched_vendor_name,
        "date": date_value,
        "due_date": due_date,
        "invoice_no": invoice_no,
        "receipt_no": receipt_no,
        "tax_amount": tax_amount,
        "total_amount": total_amount,
        "subtotal": max(total_amount - tax_amount, 0) if total_amount else 0,
        "category": category,
        "description": f"{doc_type} from {matched_vendor_name or vendor_guess}".strip(),
    }
    result["bank_details"] = {"bank_name": bank_name, "account_no": account_no, "transfer_reference": transfer_ref}
    result["receipt_details"] = {"rrn": rrn, "auth_code": auth_code, "terminal_id": terminal_id}
    result["confidence"] = {
        "vendor": 0.9 if matched_vendor_id else (0.55 if vendor_guess else 0.0),
        "date": 0.65 if date_value else 0.0,
        "total_amount": 0.8 if total_amount else 0.0,
        "invoice_no": 0.7 if invoice_no else 0.0,
        "receipt_no": 0.7 if receipt_no else 0.0,
        "tax_amount": 0.65 if tax_amount else 0.0,
        "category": 0.55 if category != "Other" else 0.25,
        "document_type": 0.75 if doc_type != "Invoice/Receipt" else 0.35,
        "payment_method": 0.7 if pay_method else 0.0,
    }
    if vendor_guess and not matched_vendor_id:
        result["warnings"].append("Vendor not found in vendor database. Consider creating a new vendor.")
    if not total_amount:
        result["warnings"].append("Total amount could not be confidently extracted; enter amount manually.")
    if doc_type == "Invoice" and not invoice_no:
        result["warnings"].append("Invoice number was not detected; enter it manually.")
    if doc_type == "Receipt" and not receipt_no:
        result["warnings"].append("Receipt/transaction reference was not detected; enter it manually.")
    return result


def duplicate_candidates(file_hash_value: str | None, amount: float | None, date_value: str | None, vendor_id: int | None):
    where = []
    params = []
    if file_hash_value:
        where.append("receipt_hash = ?")
        params.append(file_hash_value)
    if amount and date_value and vendor_id:
        where.append("(ABS(amount - ?) < 1 AND expense_date = ? AND vendor_id = ?)")
        params.extend([amount, date_value, vendor_id])
    if not where:
        return df_query("SELECT * FROM expenses WHERE 1=0")
    return df_query(f"SELECT expense_no, expense_date, amount, status FROM expenses WHERE {' OR '.join(where)}", params)


def duplicate_receipt_candidates(file_hash_value: str | None, amount: float | None, date_value: str | None, vendor_id: int | None):
    where = []
    params = []
    if file_hash_value:
        where.append("file_hash = ?")
        params.append(file_hash_value)
    if amount and date_value and vendor_id:
        where.append("(ABS(amount - ?) < 1 AND payment_date = ? AND vendor_id = ?)")
        params.extend([amount, date_value, vendor_id])
    if not where:
        return df_query("SELECT * FROM receipt_records WHERE 1=0")
    return df_query(f"SELECT receipt_no, payment_method, payment_date, amount, status FROM receipt_records WHERE {' OR '.join(where)}", params)


def match_invoice_to_po(po_id: int | None, vendor_id: int | None, amount: float | None) -> tuple[str, list[str]]:
    if not po_id:
        return "Needs Review", ["No purchase order selected."]
    po = df_query("SELECT id, po_no, vendor_id, total_amount, receiving_status FROM purchase_orders WHERE id = ?", (po_id,))
    if po.empty:
        return "Mismatch", ["Selected purchase order was not found."]
    row = po.iloc[0]
    reasons = []
    if vendor_id and int(row["vendor_id"] or 0) != int(vendor_id):
        reasons.append("Vendor differs from purchase order vendor.")
    if amount and float(amount) > float(row["total_amount"] or 0) + 1:
        reasons.append("Invoice amount is greater than purchase order amount.")
    if row["receiving_status"] not in ("Partially Received", "Fully Received"):
        reasons.append("Receiving slip is missing or goods have not been received.")
    return ("Matched" if not reasons else "Mismatch"), reasons
