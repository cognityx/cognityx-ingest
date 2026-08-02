# Provenance v1 frozen fixture

This package is the fixed source of truth for Cognityx Ingest provenance
acceptance tests. It represents a fictional HR policy designed to exercise
page labels, section boundaries, tables, figures, links, references, and
unresolved ambiguity.

## Frozen files

- `main_policy_v2.pdf` is the supplied, visually approved 19-page PDF.
- `assets/diagram.png` is the single embedded figure extracted losslessly from
  the PDF image pixels and encoded as PNG.
- `reference/provenance_test_specification.docx` is the supplied test
  specification.
- `expected/ground_truth.json` is the hand-authored test oracle.
- `expected/sha256sums.txt` records the frozen byte identities.

The PDF is the authoritative ingest input. An editable DOCX may be retained as
optional source-authoring material elsewhere, but it is not part of this frozen
acceptance fixture and is not required by its tests.

## Verification boundary

Normal tests only read these files and verify their checksums. They never
convert, rewrite, optimize, or regenerate them. The checked-in PDF was supplied
with `verified` in its filename. Its text, page geometry, links, and embedded
figure were independently inspected when this package was created. All 19 pages
were also rendered with PyMuPDF and visually inspected as a contact sheet; the
figure page was checked at higher resolution. This verifies the supplied PDF
bytes without requiring a DOCX-to-PDF conversion during tests.

## Audit notes

- 2026-08-02: corrected the native internal-link oracle to match the frozen PDF
  observation. The link rectangle covers `Appendix B` in
  `page-014:block-009` and targets Appendix B on physical page index 17. The
  PDF bytes and checksum were not changed.
