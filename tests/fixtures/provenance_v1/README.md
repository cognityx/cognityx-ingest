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

The editable `main_policy_v2.docx` was not included in the supplied files. It
must not be reconstructed from the PDF. The missing source and the required
DOCX-to-PDF regeneration check are tracked as `GAP-FIXTURE-SOURCES` in
`docs/provenance-gap-report.md`.

## Verification boundary

Normal tests only read these files and verify their checksums. They never
convert, rewrite, optimize, or regenerate them. The checked-in PDF was supplied
with `verified` in its filename. Its text, page geometry, links, and embedded
figure were independently inspected when this package was created. All 19 pages
were also rendered with PyMuPDF and visually inspected as a contact sheet; the
figure page was checked at higher resolution. This verifies the supplied PDF,
but it is not a DOCX-to-PDF equivalence check because the source DOCX was not
supplied.

When the verified DOCX becomes available, an operator must:

1. place it beside the PDF as `main_policy_v2.docx`;
2. convert it with the repository-approved Office conversion process;
3. compare the generated PDF visually with the approved PDF;
4. replace neither frozen file unless the fixture version is intentionally
   advanced; and
5. update the oracle and checksums in the same reviewed change.
