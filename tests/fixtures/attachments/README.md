# Attachment test fixtures

These files are synthetic, repository-owned fixtures. They contain only generic
order numbers, dates, and page labels:

- `selectable.pdf`: two pages with selectable text.
- `scan.pdf`: one image-only PDF page.
- `mixed.pdf`: one selectable-text page and one image-only page.
- `numeric.png` / `numeric.jpg`: local OCR smoke images.
- `blank.png` / `blurred.png`: empty and low-quality image cases.
- `encrypted.pdf`, `over-pages.pdf`, `truncated.pdf`: PDF error cases.
- `disguised.pdf`, `malformed.png`, `huge-header.png`: type and decode limits.

Run `generate_fixtures.py` from this directory to recreate the binary files.
