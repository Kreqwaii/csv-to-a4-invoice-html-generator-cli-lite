# csv-to-a4-invoice-html-generator-cli-lite

**CSV & JSON to Print-Ready A4 HTML Invoice & Quote Generator CLI (Lite)** — 100% pure Python 3.9+ standard library utility with zero external dependencies.

Convert CSV or JSON line-item files into clean, print-ready A4 HTML invoices and commercial quotes (`@page { size: A4; margin: 14mm; }`) with a light editorial layout, aligned `tabular-nums` figures, strict XSS escaping, and exact `Decimal` (`ROUND_HALF_UP`) accounting math across USD, EUR, GBP, and RUB.

---

## Features

- **Zero external dependencies**: 100% Python 3.9+ standard library (`argparse`, `csv`, `dataclasses`, `datetime`, `decimal`, `html`, `json`, `pathlib`). No `pip install` required.
- **Exact `Decimal` (`ROUND_HALF_UP`) arithmetic**: Eliminates IEEE-754 floating-point drift across line totals, per-line discounts, document-level discounts, and tax calculations.
- **Flexible CSV & JSON input**:
  - Reads UTF-8 / UTF-8-BOM CSV files (comma `,` or semicolon `;` delimited) with flexible column aliases (`name`/`item`/`title`, `description`/`details`, `qty`/`quantity`, `unit_price`/`price`/`rate`, `discount_pct`/`discount`).
  - Reads JSON either as a raw array of line items or as a full document object containing seller/client metadata, currency, tax, discount, and `items` (with CLI overrides supported).
- **Discounts & VAT/tax support**:
  - Supports per-line percentage discounts plus document-level percentage (`--discount 10%`) or fixed (`--discount 50.00`) discounts.
  - Supports exclusive (added on top) and inclusive (extracted from gross) VAT/tax rates with customizable tax labels (`--tax-label VAT`).
- **Multi-currency formatting**: Clean locale-aware formatting for `USD` (`$1,234.50`), `EUR` (`€1,234.50`), `GBP` (`£1,234.50`), and `RUB` (`1 234.50 ₽`).
- **Print-ready A4 editorial HTML**:
  - `@page { size: A4; margin: 14mm; }` print styling with `break-inside: avoid` rules for line rows, totals card, and notes.
  - `font-variant-numeric: tabular-nums` on every quantity, unit price, discount, tax, and total cell.
  - Full HTML escaping (`html.escape(..., quote=True)`) on all user-supplied fields to prevent XSS when rendering untrusted CSV/JSON inputs.
- **Automation-friendly CLI**: Supports file output (`-o invoice.html`), `stdout` piping, `stdin` input (`-`), and machine-readable `--format json` output.

---

## Quick Start

Generate a print-ready A4 HTML invoice from the bundled CSV sample:

```bash
python cli.py examples/sample_items.csv -o invoice.html \
  --number INV-2026-001 \
  --issue-date 2026-03-15 \
  --due-date 2026-03-29 \
  --currency USD \
  --discount 10% \
  --vat 20 \
  --seller-name "Northwind Systems Ltd." \
  --client-name "Acme Retail Corp" \
  --notes "Payment due within 14 days via bank transfer."
```

Open `invoice.html` in any browser and press `Ctrl+P` / `Cmd+P` -> **Save as PDF** (Paper size: A4).

Generate a commercial quote from the bundled JSON document:

```bash
python cli.py examples/sample_invoice.json -o quote.html --doc-type quote --number QUO-2026-014
```

Emit a JSON calculation breakdown for downstream billing pipelines:

```bash
python cli.py examples/sample_items.csv --currency EUR --discount 150.00 --vat 19 --format json
```

Pipe CSV data from `stdin` directly to an HTML file:

```bash
cat examples/sample_items.csv | python cli.py - --input-format csv --currency GBP --vat 20 > invoice.html
```

Run the offline unit test suite:

```bash
py -m unittest discover -s tests -v
```

---

## Input File Formats

### CSV Schema

Required columns: `name` (or `item` / `title` / `service`) and `unit_price` (or `price` / `rate`).
Optional columns: `description` (`details`), `qty` (`quantity`, default `1`), `unit` (`uom`), `discount_pct` (`discount`, default `0`).

```csv
name,description,qty,unit,unit_price,discount_pct
Backend API Architecture,REST endpoints and schema design,12,hrs,95.00,0
Frontend Dashboard Implementation,Responsive A4 report layout and state logic,16,hrs,85.00,10
```

### JSON Schema

You can pass either a JSON array of item objects or a structured document object (see `examples/sample_invoice.json`):

```json
{
  "doc_type": "invoice",
  "doc_number": "INV-2026-042",
  "issue_date": "2026-03-15",
  "due_date": "2026-03-29",
  "currency": "EUR",
  "discount": "5%",
  "tax_rate": "19",
  "tax_mode": "exclusive",
  "seller": { "name": "Northwind Systems Ltd.", "email": "billing@example.com" },
  "client": { "name": "Contoso Retail GmbH", "email": "accounts@example.org" },
  "items": [
    { "name": "API Integration Sprint", "qty": 10, "unit": "hrs", "unit_price": "110.00" }
  ]
}
```

---

## CLI Options & Exit Codes

| Flag | Default | Description |
| :--- | :--- | :--- |
| `input` | `—` | Path to CSV/JSON line-item file (use `-` for stdin) |
| `-o`, `--output` | `stdout` | Output file path (use `-` for stdout) |
| `--input-format` | `auto` | Input format: `auto`, `csv`, or `json` |
| `--format` | `html` | Output format: `html` (A4 print-ready) or `json` (summary) |
| `--doc-type` | `invoice` | Document type: `invoice` or `quote` |
| `--number` | `INV-001` / `QUO-001` | Invoice or quote document number |
| `--issue-date` | today (`YYYY-MM-DD`) | Issue date shown in document header |
| `--due-date` | `—` | Due date (invoice) or valid-until date (quote) |
| `--currency` | `USD` | Currency formatting: `USD`, `EUR`, `GBP`, or `RUB` |
| `--discount` | `0` | Document discount (`10%` for percentage, `50.00` for fixed) |
| `--discount-type` | `auto` | Interpret `--discount` as `auto`, `percent`, `fixed`, or `none` |
| `--tax`, `--vat` | `0` | VAT/tax rate percentage |
| `--tax-mode` | `exclusive` | `exclusive` (added on top) or `inclusive` (extracted from total) |
| `--tax-label` | `VAT` | Label displayed on the tax summary line |
| `--seller-*` | `—` | `--seller-name`, `--seller-address`, `--seller-email`, `--seller-tax-id` |
| `--client-*` | `—` | `--client-name`, `--client-address`, `--client-email`, `--client-tax-id` |
| `--notes` | `—` | Payment instructions or quote terms |

### Exit Codes

- `0`: Document generated successfully
- `1`: Input validation or accounting error (empty items, malformed CSV/JSON, negative quantities, fixed discount exceeding subtotal)
- `2`: Invalid CLI arguments, unreadable input file, or unwritable output path

---

## Need a Complete Self-Hosted Invoice, Quote & Payment Tracker Web Studio?

This Lite CLI generates single A4 HTML invoices and quotes from static CSV or JSON files. When you need a full billing workflow to **manage clients, convert quotes into invoices, record partial payments, and track overdue balances locally**, check out **Self-Hosted Invoice, Quote & Payment Tracker Studio**:

| Capability | Lite CLI (Free / MIT) | Self-Hosted Invoice, Quote & Payment Tracker Studio (Pro) |
| :--- | :--- | :--- |
| CSV/JSON line items to A4 Print HTML (`Decimal` math) | Yes | Yes |
| Multi-currency (`USD`, `EUR`, `GBP`, `RUB`), discounts & VAT | Yes | Yes |
| Light local Web UI (zero external dependencies) | — | Yes |
| Client CRUD directory & reusable billing profiles | — | Yes |
| Sequential `INV-` / `QUO-` numbering | — | Yes |
| 1-click quote-to-invoice conversion | — | Yes |
| Partial payments & automatic overdue tracking | — | Yes |
| Persistent local storage | — | SQLite WAL persistence |
| Bilingual EN/RU A4 Print-to-PDF & CSV/JSON import/export | — | Yes |
| Verification coverage | 18 offline unit tests | 28 built-in self-tests |

- **Get Self-Hosted Invoice, Quote & Payment Tracker Studio:** https://funpay.com/lots/offer?id=82603441

---

## License

MIT License — see [LICENSE](LICENSE).
