#!/usr/bin/env python3
"""CLI entrypoint for csv-to-a4-invoice-html-generator-cli-lite.

Converts CSV or JSON line-item files into print-ready A4 editorial HTML
invoices and commercial quotes using 100% pure Python 3.9+ stdlib.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import List, Optional, Sequence

from invoice_gen import (
    SUPPORTED_CURRENCIES,
    SUPPORTED_DOC_TYPES,
    SUPPORTED_TAX_MODES,
    build_config_from_metadata,
    build_invoice_document,
    format_money,
    load_invoice_input,
    render_a4_html,
)

__version__ = "1.0.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="invoice-gen",
        description=(
            "Convert CSV or JSON line-item files into clean, print-ready A4 HTML "
            "invoices or quotes with exact Decimal math."
        ),
    )
    parser.add_argument(
        "input",
        help="Path to input CSV or JSON line-item file (use '-' to read from stdin).",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="Write output to file path (defaults to stdout; use '-' for stdout).",
    )
    parser.add_argument(
        "--input-format",
        choices=("auto", "csv", "json"),
        default="auto",
        help="Input file format: 'auto' (default), 'csv', or 'json'.",
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("html", "json"),
        default="html",
        help="Output format: 'html' (A4 print-ready HTML, default) or 'json' (summary).",
    )
    parser.add_argument(
        "--doc-type",
        choices=SUPPORTED_DOC_TYPES,
        default=None,
        help="Document type: 'invoice' or 'quote' (default: invoice).",
    )
    parser.add_argument(
        "--number",
        dest="doc_number",
        default=None,
        help="Invoice or quote number (e.g. INV-2026-001).",
    )
    parser.add_argument(
        "--issue-date",
        default=None,
        help="Issue date string (default: today's ISO date YYYY-MM-DD).",
    )
    parser.add_argument(
        "--due-date",
        default=None,
        help="Due date (for invoice) or Valid Until date (for quote).",
    )
    parser.add_argument(
        "--currency",
        choices=SUPPORTED_CURRENCIES,
        default=None,
        help="Currency code: USD, EUR, GBP, or RUB (default: USD).",
    )
    parser.add_argument(
        "--discount",
        default=None,
        help="Document discount: percentage like '10%%' or fixed amount like '50.00'.",
    )
    parser.add_argument(
        "--discount-type",
        choices=("auto", "percent", "fixed", "none"),
        default=None,
        help="Interpret --discount as 'auto' (default), 'percent', 'fixed', or 'none'.",
    )
    parser.add_argument(
        "--tax",
        "--vat",
        dest="tax_rate",
        default=None,
        help="VAT/tax rate percentage (e.g. '20', '7.5', '0'). Default: 0.",
    )
    parser.add_argument(
        "--tax-mode",
        choices=SUPPORTED_TAX_MODES,
        default=None,
        help="Tax calculation mode: 'exclusive' (added on top, default) or 'inclusive'.",
    )
    parser.add_argument(
        "--tax-label",
        default=None,
        help="Label printed for the tax line (default: 'VAT').",
    )
    parser.add_argument("--seller-name", default=None, help="Issuer/seller company or name.")
    parser.add_argument("--seller-address", default=None, help="Issuer/seller street address.")
    parser.add_argument("--seller-email", default=None, help="Issuer/seller contact email.")
    parser.add_argument("--seller-tax-id", default=None, help="Issuer/seller tax or VAT ID.")
    parser.add_argument("--client-name", default=None, help="Client/recipient company or name.")
    parser.add_argument("--client-address", default=None, help="Client/recipient street address.")
    parser.add_argument("--client-email", default=None, help="Client/recipient contact email.")
    parser.add_argument("--client-tax-id", default=None, help="Client/recipient tax or VAT ID.")
    parser.add_argument("--notes", default=None, help="Payment instructions or quote notes.")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.input == "-":
        raw_text = sys.stdin.read()
        source_name: Optional[str] = "-"
    else:
        input_path = Path(args.input)
        if not input_path.is_file():
            print(f"Error: input file not found: {args.input}", file=sys.stderr)
            return 2
        try:
            raw_text = input_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error reading '{args.input}': {exc}", file=sys.stderr)
            return 2
        source_name = str(input_path)

    try:
        items, metadata = load_invoice_input(
            raw_text,
            source_name=source_name,
            input_format=args.input_format,
        )
        overrides = {
            "doc_type": args.doc_type,
            "doc_number": args.doc_number,
            "issue_date": args.issue_date,
            "due_date": args.due_date,
            "currency": args.currency,
            "discount": args.discount,
            "discount_type": args.discount_type,
            "tax_rate_pct": args.tax_rate,
            "tax_mode": args.tax_mode,
            "tax_label": args.tax_label,
            "seller_name": args.seller_name,
            "seller_address": args.seller_address,
            "seller_email": args.seller_email,
            "seller_tax_id": args.seller_tax_id,
            "client_name": args.client_name,
            "client_address": args.client_address,
            "client_email": args.client_email,
            "client_tax_id": args.client_tax_id,
            "notes": args.notes,
        }
        config = build_config_from_metadata(metadata, overrides=overrides)
        doc = build_invoice_document(items, config)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.output_format == "json":
        rendered = json.dumps(doc.to_dict(), indent=2, ensure_ascii=False)
    else:
        rendered = render_a4_html(doc)

    if args.output and args.output != "-":
        out_path = Path(args.output)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            print(f"Error writing '{args.output}': {exc}", file=sys.stderr)
            return 2
        total_fmt = format_money(doc.summary.grand_total, doc.summary.currency)
        print(
            f"Generated {doc.config.doc_type} {doc.config.doc_number} -> {out_path} "
            f"(Total: {total_fmt})"
        )
    else:
        print(rendered)

    return 0


if __name__ == "__main__":
    sys.exit(main())
