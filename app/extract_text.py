"""Read text from PDF / Word locally. Scanned pages are OCR'd on this computer (RapidOCR, offline)."""
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # older PyMuPDF
    import fitz

_ocr_engine = None


def _ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine


def _layout_from_boxes(boxes, page_width):
    """Rebuild reading order from OCR boxes: group into rows by y, order by x, keep column spacing."""
    items = []
    for box, text, _score in boxes:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append({"x": min(xs), "y": (min(ys) + max(ys)) / 2, "h": max(ys) - min(ys), "t": text})
    items.sort(key=lambda i: i["y"])
    rows = []
    for it in items:
        if rows and abs(it["y"] - rows[-1]["y"]) < max(8, 0.5 * min(it["h"], rows[-1]["h"])):
            rows[-1]["items"].append(it)
        else:
            rows.append({"y": it["y"], "h": it["h"], "items": [it]})
    char_w = page_width / 110.0
    lines = []
    for r in rows:
        line = ""
        for it in sorted(r["items"], key=lambda i: i["x"]):
            col = int(it["x"] / char_w)
            if len(line) < col:
                line += " " * (col - len(line))
            elif line:
                line += "  "
            line += it["t"]
        lines.append(line.rstrip())
    return "\n".join(lines)


def read_pdf(path, on_progress=None):
    """Return list of (page_no, text, is_ocr)."""
    doc = fitz.open(path)
    out = []
    n = len(doc)
    for i, page in enumerate(doc):
        text = page.get_text("text") or ""
        is_ocr = False
        if len(text.strip()) < 40:  # scanned / image-only page
            if on_progress:
                on_progress(f"OCR {i + 1}/{n}")
            pix = page.get_pixmap(dpi=200)
            res, _ = _ocr()(pix.tobytes("png"))
            text = _layout_from_boxes(res or [], pix.width)
            is_ocr = True
        elif on_progress:
            on_progress(f"Text {i + 1}/{n}")
        out.append((i + 1, text, is_ocr))
    doc.close()
    return out


def read_docx(path):
    import docx
    d = docx.Document(path)
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    return [(1, "\n".join(parts), False)]


class UnreadableFile(Exception):
    """A file that cannot be read; the message is meant for the user."""


def check_readable(ext, content):
    """Raise UnreadableFile if the uploaded bytes cannot be read as this type. Called before anything is stored."""
    if not content:
        raise UnreadableFile("文件是空的 / The file is empty")
    if ext == ".pdf":
        try:
            doc = fitz.open(stream=content, filetype="pdf")
        except Exception:
            raise UnreadableFile("PDF 无法打开，可能已损坏或不是真正的 PDF。请重新导出或扫描后再上传 / "
                                 "The PDF cannot be opened; it may be damaged or not a real PDF. Export or scan it again")
        try:
            if doc.needs_pass:
                raise UnreadableFile("PDF 设置了打开密码，请先去掉密码再上传 / The PDF is password-protected; remove the password first")
            if doc.page_count < 1:
                raise UnreadableFile("PDF 里没有页面 / The PDF has no pages")
        finally:
            doc.close()
    elif ext == ".docx":
        import io
        import zipfile
        try:
            import docx
            docx.Document(io.BytesIO(content))
        except (zipfile.BadZipFile, KeyError, ValueError, Exception):
            raise UnreadableFile("Word 文件无法打开，可能已损坏，或是旧版 .doc 改了扩展名。请用 Word 另存为 .docx 后再上传 / "
                                 "The Word file cannot be opened; it may be damaged or an old .doc renamed. Save it as .docx again")
    elif ext == ".txt":
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            raise UnreadableFile("文本文件不是 UTF-8 编码，请另存为 UTF-8 后再上传 / The text file is not UTF-8; save it as UTF-8")


def read_any(path, on_progress=None):
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return read_pdf(path, on_progress)
    if ext == ".docx":
        return read_docx(path)
    if ext == ".txt":
        return [(1, Path(path).read_text(encoding="utf-8", errors="ignore"), False)]
    raise ValueError(f"Unsupported file type: {ext}")
