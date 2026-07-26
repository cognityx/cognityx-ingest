"""PDF extraction boundary; deterministic parsing is the default."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pypdf import PdfReader


class UnsupportedInputError(ValueError):
    """Raised when a requested source is not an ingestible PDF."""


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    page_number: int
    text: str


class PdfExtractor(Protocol):
    def extract(self, path: Path) -> tuple[ExtractedPage, ...]: ...


class PyPdfExtractor:
    """Extract page text using pypdf without an external service or model."""

    def extract(self, path: Path) -> tuple[ExtractedPage, ...]:
        if path.suffix.lower() != ".pdf":
            raise UnsupportedInputError(f"Only PDF input is supported: {path}")
        try:
            reader = PdfReader(path)
            return tuple(
                ExtractedPage(index, (page.extract_text() or "").strip())
                for index, page in enumerate(reader.pages, start=1)
            )
        except Exception as error:
            raise UnsupportedInputError(f"Could not parse PDF {path}: {error}") from error
