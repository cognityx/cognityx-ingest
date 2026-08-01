# Provenance v1 frozen fixture

This package is the fixed source of truth for Cognityx Ingest provenance
acceptance tests. It represents a fictional HR policy designed to exercise
page labels, section boundaries, tables, figures, links, references, and
unresolved ambiguity.

## Frozen files

- `main_policy_v2.pdf` is the supplied, visually approved 19-page PDF.
- `main_policy_v2.docx` is the exact byte-for-byte copy requested as the
  authoritative source fixture. The supplied bytes are still a PDF 1.7
  container; changing the filename does not convert them to OOXML.
- `assets/diagram.png` is the single embedded figure extracted losslessly from
  the PDF image pixels and encoded as PNG.
- `reference/provenance_test_specification.docx` is the supplied test
  specification.
- `expected/ground_truth.json` is the hand-authored test oracle.
- `expected/sha256sums.txt` records the frozen byte identities.

No DOCX reconstruction or PDF-to-DOCX conversion was performed. Both named
fixture files intentionally have the same SHA-256 because the correction
required an exact copy of the supplied file.

## Verification boundary

Normal tests only read these files and verify their checksums. They never
convert, rewrite, optimize, or regenerate them. The checked-in PDF was supplied
with `verified` in its filename. Its text, page geometry, links, and embedded
figure were independently inspected when this package was created. All 19 pages
were also rendered with PyMuPDF and visually inspected as a contact sheet; the
figure page was checked at higher resolution. This verifies the supplied PDF
bytes. It does not claim that `main_policy_v2.docx` is an editable OOXML file or
that a DOCX-to-PDF conversion was performed.
