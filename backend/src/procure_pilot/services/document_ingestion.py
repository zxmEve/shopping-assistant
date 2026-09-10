from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Protocol

import pypdfium2 as pdfium
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pydantic import BaseModel, Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError

PARSER_VERSION = "policy-file-parser-v1"
PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_MEDIA_TYPES = frozenset({"text/plain", "text/markdown"})
SUPPORTED_MEDIA_TYPES = frozenset({PDF_MEDIA_TYPE, DOCX_MEDIA_TYPE, *TEXT_MEDIA_TYPES})


class DocumentParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ParsedPolicyFile(BaseModel):
    raw_content: str
    file_name: str
    media_type: str
    file_hash: str
    file_size: int = Field(ge=0)
    parser_version: str = PARSER_VERSION
    page_count: int | None = Field(default=None, ge=0)
    table_count: int = Field(default=0, ge=0)
    ocr_pages: list[int] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def extraction_metadata(self) -> dict:
        return {
            "page_count": self.page_count,
            "table_count": self.table_count,
            "ocr_pages": self.ocr_pages,
            "warnings": self.warnings,
            "extracted_chars": len(self.raw_content),
        }


class OcrProvider(Protocol):
    name: str

    def extract_pdf_page(self, pdf_bytes: bytes, page_index: int) -> str: ...


class TesseractOcrProvider:
    name = "tesseract"

    def __init__(self, *, languages: str = "chi_sim+eng", timeout_seconds: float = 30.0) -> None:
        if not shutil.which("tesseract"):
            raise RuntimeError("Tesseract executable is not installed")
        self.languages = languages
        self.timeout_seconds = timeout_seconds

    def extract_pdf_page(self, pdf_bytes: bytes, page_index: int) -> str:
        try:
            document = pdfium.PdfDocument(pdf_bytes)
            page = document[page_index]
            bitmap = page.render(scale=3)
            image = bitmap.to_pil()
            png = io.BytesIO()
            image.save(png, format="PNG")
            completed = subprocess.run(
                ["tesseract", "stdin", "stdout", "-l", self.languages],
                input=png.getvalue(),
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            raise DocumentParseError("OCR_FAILED", "OCR execution failed") from exc
        finally:
            for resource_name in ("image", "bitmap", "page", "document"):
                resource = locals().get(resource_name)
                close = getattr(resource, "close", None)
                if close is not None:
                    close()
        if completed.returncode != 0:
            raise DocumentParseError("OCR_FAILED", "OCR execution failed")
        try:
            return completed.stdout.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise DocumentParseError("OCR_FAILED", "OCR returned invalid UTF-8") from exc


class PolicyFileParser:
    def __init__(
        self,
        *,
        ocr_provider: OcrProvider | None = None,
        max_file_bytes: int = 10 * 1024 * 1024,
        max_pdf_pages: int = 200,
        max_docx_uncompressed_bytes: int = 50 * 1024 * 1024,
        max_docx_entries: int = 5_000,
    ) -> None:
        self.ocr_provider = ocr_provider
        self.max_file_bytes = max_file_bytes
        self.max_pdf_pages = max_pdf_pages
        self.max_docx_uncompressed_bytes = max_docx_uncompressed_bytes
        self.max_docx_entries = max_docx_entries

    def parse(self, data: bytes, *, file_name: str, media_type: str) -> ParsedPolicyFile:
        safe_name = Path(file_name).name
        if not safe_name:
            raise DocumentParseError("INVALID_FILE_NAME", "A source file name is required")
        if not data:
            raise DocumentParseError("EMPTY_FILE", "The source file is empty")
        if len(data) > self.max_file_bytes:
            raise DocumentParseError(
                "KNOWLEDGE_FILE_TOO_LARGE",
                f"The source file exceeds {self.max_file_bytes} bytes",
            )
        normalized_media_type = media_type.split(";", 1)[0].strip().lower()
        if normalized_media_type not in SUPPORTED_MEDIA_TYPES:
            raise DocumentParseError(
                "UNSUPPORTED_KNOWLEDGE_FILE",
                "Only PDF, DOCX, UTF-8 text, and Markdown policy files are supported",
            )
        self._validate_signature(data, safe_name, normalized_media_type)

        if normalized_media_type == PDF_MEDIA_TYPE:
            content, page_count, ocr_pages, warnings = self._parse_pdf(data)
            table_count = 0
        elif normalized_media_type == DOCX_MEDIA_TYPE:
            content, table_count = self._parse_docx(data)
            page_count, ocr_pages, warnings = None, [], []
        else:
            try:
                content = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise DocumentParseError(
                    "INVALID_TEXT_ENCODING", "Text policy files must use UTF-8"
                ) from exc
            page_count, table_count, ocr_pages, warnings = None, 0, [], []

        if len(content.strip()) < 20:
            raise DocumentParseError(
                "INSUFFICIENT_EXTRACTED_TEXT",
                "The source file did not contain enough policy text",
            )
        return ParsedPolicyFile(
            raw_content=content,
            file_name=safe_name,
            media_type=normalized_media_type,
            file_hash=hashlib.sha256(data).hexdigest(),
            file_size=len(data),
            page_count=page_count,
            table_count=table_count,
            ocr_pages=ocr_pages,
            warnings=warnings,
        )

    @staticmethod
    def _validate_signature(data: bytes, file_name: str, media_type: str) -> None:
        extension = Path(file_name).suffix.lower()
        expected_extensions = {
            PDF_MEDIA_TYPE: {".pdf"},
            DOCX_MEDIA_TYPE: {".docx"},
            "text/plain": {".txt"},
            "text/markdown": {".md", ".markdown"},
        }[media_type]
        if extension not in expected_extensions:
            raise DocumentParseError(
                "FILE_TYPE_MISMATCH", "File extension does not match the declared media type"
            )
        if media_type == PDF_MEDIA_TYPE and not data.startswith(b"%PDF-"):
            raise DocumentParseError("FILE_TYPE_MISMATCH", "The uploaded file is not a PDF")
        if media_type == DOCX_MEDIA_TYPE and not data.startswith(b"PK"):
            raise DocumentParseError("FILE_TYPE_MISMATCH", "The uploaded file is not a DOCX")

    def _parse_pdf(self, data: bytes) -> tuple[str, int, list[int], list[str]]:
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
        except (PdfReadError, OSError, ValueError) as exc:
            raise DocumentParseError("INVALID_PDF", "The PDF could not be parsed") from exc
        if reader.is_encrypted:
            raise DocumentParseError("ENCRYPTED_PDF", "Encrypted PDF files are not supported")
        if len(reader.pages) > self.max_pdf_pages:
            raise DocumentParseError(
                "DOCUMENT_TOO_MANY_PAGES",
                f"The PDF exceeds {self.max_pdf_pages} pages",
            )

        parts: list[str] = []
        ocr_pages: list[int] = []
        warnings: list[str] = []
        for page_index, page in enumerate(reader.pages):
            try:
                text = (page.extract_text() or "").strip()
            except (PdfReadError, KeyError, ValueError) as exc:
                raise DocumentParseError("INVALID_PDF", "A PDF page could not be parsed") from exc
            page_number = page_index + 1
            if len(text) < 12 and self._page_has_image(page):
                if self.ocr_provider is None:
                    raise DocumentParseError(
                        "OCR_REQUIRED",
                        f"PDF page {page_number} appears scanned; configure an OCR provider",
                    )
                text = self.ocr_provider.extract_pdf_page(data, page_index).strip()
                ocr_pages.append(page_number)
                if len(text) < 12:
                    raise DocumentParseError(
                        "INSUFFICIENT_OCR_TEXT",
                        f"OCR did not recover enough text from PDF page {page_number}",
                    )
            elif not text:
                warnings.append(f"empty_page:{page_number}")
                continue
            parts.append(f"# 第 {page_number} 页\n{text}")
        if ocr_pages:
            warnings.append(f"ocr_provider:{self.ocr_provider.name}")
        return "\n\n".join(parts), len(reader.pages), ocr_pages, warnings

    @staticmethod
    def _page_has_image(page) -> bool:
        try:
            resources = page.get("/Resources")
            if resources is None:
                return False
            resources = resources.get_object()
            xobjects = resources.get("/XObject")
            if xobjects is None:
                return False
            return any(
                item.get_object().get("/Subtype") == "/Image"
                for item in xobjects.get_object().values()
            )
        except (AttributeError, KeyError, TypeError):
            return False

    def _parse_docx(self, data: bytes) -> tuple[str, int]:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > self.max_docx_entries:
                    raise DocumentParseError(
                        "DOCX_ARCHIVE_LIMIT", "The DOCX archive contains too many entries"
                    )
                if "[Content_Types].xml" not in archive.namelist() or not any(
                    name.startswith("word/document.xml") for name in archive.namelist()
                ):
                    raise DocumentParseError("INVALID_DOCX", "The file is not a valid DOCX")
                expanded = sum(entry.file_size for entry in entries)
                if expanded > self.max_docx_uncompressed_bytes:
                    raise DocumentParseError(
                        "DOCX_ARCHIVE_LIMIT", "The DOCX archive expands beyond the safe limit"
                    )
            document = Document(io.BytesIO(data))
        except DocumentParseError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise DocumentParseError("INVALID_DOCX", "The DOCX could not be parsed") from exc

        parts: list[str] = []
        table_count = 0
        for block in document.iter_inner_content():
            if isinstance(block, Paragraph):
                text = block.text.strip()
                if not text:
                    continue
                style_name = block.style.name if block.style is not None else ""
                if style_name.lower().startswith("heading"):
                    suffix = style_name.removeprefix("Heading").strip()
                    level = int(suffix) if suffix.isdigit() else 2
                    parts.append(f"{'#' * min(max(level, 1), 6)} {text}")
                else:
                    parts.append(text)
            elif isinstance(block, Table):
                table_count += 1
                rendered = self._render_table(block)
                if rendered:
                    parts.append(f"## 表格 {table_count}\n{rendered}")
        return "\n\n".join(parts), table_count

    @staticmethod
    def _render_table(table: Table) -> str:
        rows = [
            [" ".join(cell.text.split()).replace("|", "\\|") for cell in row.cells]
            for row in table.rows
        ]
        rows = [row for row in rows if any(row)]
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        normalized = [row + [""] * (width - len(row)) for row in rows]
        lines = ["| " + " | ".join(normalized[0]) + " |"]
        lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
        lines.extend("| " + " | ".join(row) + " |" for row in normalized[1:])
        return "\n".join(lines)
