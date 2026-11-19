#!/usr/bin/env python3
"""
invoice_gen.py — Pure Python 3.9+ standard-library core for converting CSV or
JSON line-item files into print-ready A4 HTML invoices and commercial quotes.

Features:
- Exact Decimal arithmetic with ROUND_HALF_UP rounding
- Supports USD, EUR, GBP, and RUB currency formatting
- Per-line % discounts and document-level % or fixed discounts
- Exclusive or inclusive VAT/tax calculation
- Strict HTML escaping (XSS-safe) for every user-supplied field
- Print-ready A4 CSS layout (@page { size: A4; margin: 14mm; }, tabular-nums)
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import html
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

SUPPORTED_CURRENCIES = ("USD", "EUR", "GBP", "RUB")
SUPPORTED_DOC_TYPES = ("invoice", "quote")
SUPPORTED_TAX_MODES = ("exclusive", "inclusive")
SUPPORTED_DISCOUNT_TYPES = ("none", "percent", "fixed")

MONEY_QUANT = Decimal("0.01")
ZERO = Decimal("0.00")


def to_decimal(value: Union[str, int, float, Decimal], field_name: str = "value") -> Decimal:
    """Parse a finite numeric value into a Decimal without float drift."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Invalid numeric value for '{field_name}': {value!r}")

    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, float):
        dec = Decimal(str(value))
    elif isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"Empty numeric value for '{field_name}'.")
        try:
            dec = Decimal(cleaned)
        except InvalidOperation as exc:
            raise ValueError(f"Invalid numeric value for '{field_name}': {value!r}") from exc
    else:
        raise ValueError(f"Unsupported type for '{field_name}': {type(value).__name__}")

    if not dec.is_finite():
        raise ValueError(f"Non-finite numeric value for '{field_name}': {value!r}")
    return dec


def round_money(value: Decimal) -> Decimal:
    """Round a Decimal to 2 decimal places using ROUND_HALF_UP."""
    rounded = value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    return ZERO if rounded == 0 else rounded


def format_decimal_compact(value: Decimal) -> str:
    """Format a quantity or rate without unnecessary trailing zeros."""
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_money(amount: Decimal, currency: str) -> str:
    """Format a Decimal monetary amount according to currency conventions."""
    code = currency.upper().strip()
    if code not in SUPPORTED_CURRENCIES:
        raise ValueError(
            f"Unsupported currency '{currency}'. Supported: {', '.join(SUPPORTED_CURRENCIES)}."
        )
    rounded = round_money(amount)
    sign = "-" if rounded < 0 else ""
    abs_val = abs(rounded)

    if code == "USD":
        return f"{sign}${abs_val:,.2f}"
    if code == "EUR":
        return f"{sign}€{abs_val:,.2f}"
    if code == "GBP":
        return f"{sign}£{abs_val:,.2f}"
    # RUB
    grouped = f"{abs_val:,.2f}".replace(",", " ")
    return f"{sign}{grouped} ₽"


def parse_discount(spec: Union[str, int, float, Decimal], discount_type: str = "auto") -> Tuple[str, Decimal]:
    """Parse a discount specification ('10%', '50.00', '0') into (type, Decimal)."""
    mode = discount_type.lower().strip()
    if mode not in ("auto", "none", "percent", "fixed"):
        raise ValueError(
            f"Invalid discount type '{discount_type}'. Choose from auto, none, percent, fixed."
        )

    if mode == "none":
        return ("none", ZERO)

    raw = str(spec).strip()
    if not raw:
        return ("none", ZERO)

    has_pct = raw.endswith("%")
    num_part = raw[:-1].strip() if has_pct else raw
    val = to_decimal(num_part, "discount")
    if val < 0:
        raise ValueError("Discount cannot be negative.")

    if has_pct:
        if mode == "fixed":
            raise ValueError("Cannot use '%' suffix when discount_type is 'fixed'.")
        if val > Decimal("100"):
            raise ValueError("Percentage discount cannot exceed 100%.")
        return ("none", ZERO) if val == 0 else ("percent", val)

    if mode == "percent":
        if val > Decimal("100"):
            raise ValueError("Percentage discount cannot exceed 100%.")
        return ("none", ZERO) if val == 0 else ("percent", val)

    if val == 0:
        return ("none", ZERO)
    return ("fixed", round_money(val))


@dataclass
class PartyInfo:
    name: str = ""
    address: str = ""
    email: str = ""
    tax_id: str = ""

    def has_content(self) -> bool:
        return any(
            bool(v.strip())
            for v in (self.name, self.address, self.email, self.tax_id)
        )


@dataclass
class LineItem:
    name: str
    qty: Decimal
    unit_price: Decimal
    unit: str = ""
    description: str = ""
    discount_pct: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        self.name = str(self.name).strip()
        if not self.name:
            raise ValueError("Line item name cannot be empty.")
        self.qty = to_decimal(self.qty, "qty")
        self.unit_price = to_decimal(self.unit_price, "unit_price")
        self.discount_pct = to_decimal(self.discount_pct, "discount_pct")
        self.unit = str(self.unit or "").strip()
        self.description = str(self.description or "").strip()

        if self.qty < 0:
            raise ValueError(f"Quantity cannot be negative for item '{self.name}'.")
        if self.unit_price < 0:
            raise ValueError(f"Unit price cannot be negative for item '{self.name}'.")
        if not (Decimal("0") <= self.discount_pct <= Decimal("100")):
            raise ValueError(
                f"Line discount_pct must be between 0 and 100 for item '{self.name}'."
            )

    @property
    def gross(self) -> Decimal:
        return round_money(self.qty * self.unit_price)

    @property
    def discount_amount(self) -> Decimal:
        if self.discount_pct == 0:
            return ZERO
        return round_money(self.gross * (self.discount_pct / Decimal("100")))

    @property
    def line_total(self) -> Decimal:
        return round_money(self.gross - self.discount_amount)


@dataclass
class InvoiceConfig:
    doc_type: str = "invoice"
    doc_number: str = ""
    issue_date: str = ""
    due_date: str = ""
    currency: str = "USD"
    discount_type: str = "none"
    discount_value: Decimal = ZERO
    tax_rate_pct: Decimal = Decimal("0")
    tax_mode: str = "exclusive"
    tax_label: str = "VAT"
    seller: PartyInfo = field(default_factory=PartyInfo)
    client: PartyInfo = field(default_factory=PartyInfo)
    notes: str = ""

    def __post_init__(self) -> None:
        self.doc_type = self.doc_type.lower().strip()
        if self.doc_type not in SUPPORTED_DOC_TYPES:
            raise ValueError(
                f"Unsupported doc_type '{self.doc_type}'. Choose from {', '.join(SUPPORTED_DOC_TYPES)}."
            )
        self.currency = self.currency.upper().strip()
        if self.currency not in SUPPORTED_CURRENCIES:
            raise ValueError(
                f"Unsupported currency '{self.currency}'. Choose from {', '.join(SUPPORTED_CURRENCIES)}."
            )
        if not self.doc_number.strip():
            self.doc_number = "QUO-001" if self.doc_type == "quote" else "INV-001"
        else:
            self.doc_number = self.doc_number.strip()

        if not self.issue_date.strip():
            self.issue_date = date.today().isoformat()
        else:
            self.issue_date = self.issue_date.strip()
        self.due_date = self.due_date.strip()

        self.discount_type = self.discount_type.lower().strip()
        if self.discount_type not in SUPPORTED_DISCOUNT_TYPES:
            raise ValueError(
                f"Unsupported discount_type '{self.discount_type}'. Choose from {', '.join(SUPPORTED_DISCOUNT_TYPES)}."
            )
        self.discount_value = to_decimal(self.discount_value, "discount_value")
        if self.discount_value < 0:
            raise ValueError("Document discount cannot be negative.")
        if self.discount_type == "percent" and self.discount_value > Decimal("100"):
            raise ValueError("Percentage discount cannot exceed 100%.")
        if self.discount_type == "fixed":
            self.discount_value = round_money(self.discount_value)
        if self.discount_value == 0:
            self.discount_type = "none"
            self.discount_value = ZERO

        self.tax_rate_pct = to_decimal(self.tax_rate_pct, "tax_rate_pct")
        if self.tax_rate_pct < 0:
            raise ValueError("Tax rate cannot be negative.")

        self.tax_mode = self.tax_mode.lower().strip()
        if self.tax_mode not in SUPPORTED_TAX_MODES:
            raise ValueError(
                f"Unsupported tax_mode '{self.tax_mode}'. Choose from {', '.join(SUPPORTED_TAX_MODES)}."
            )
        self.tax_label = self.tax_label.strip() or "VAT"


@dataclass
class InvoiceSummary:
    currency: str
    item_count: int
    subtotal_gross: Decimal
    line_discounts: Decimal
    subtotal: Decimal
    discount_type: str
    discount_value: Decimal
    discount_amount: Decimal
    taxable_base: Decimal
    tax_label: str
    tax_rate_pct: Decimal
    tax_mode: str
    tax_amount: Decimal
    grand_total: Decimal


@dataclass
class InvoiceDocument:
    config: InvoiceConfig
    items: List[LineItem]
    summary: InvoiceSummary

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config": {
                "doc_type": self.config.doc_type,
                "doc_number": self.config.doc_number,
                "issue_date": self.config.issue_date,
                "due_date": self.config.due_date,
                "currency": self.config.currency,
                "discount_type": self.config.discount_type,
                "discount_value": str(self.config.discount_value),
                "tax_rate_pct": str(self.config.tax_rate_pct),
                "tax_mode": self.config.tax_mode,
                "tax_label": self.config.tax_label,
                "seller": asdict(self.config.seller),
                "client": asdict(self.config.client),
                "notes": self.config.notes,
            },
            "items": [
                {
                    "name": item.name,
                    "description": item.description,
                    "qty": format_decimal_compact(item.qty),
                    "unit": item.unit,
                    "unit_price": str(round_money(item.unit_price)),
                    "discount_pct": format_decimal_compact(item.discount_pct),
                    "gross": str(item.gross),
                    "discount_amount": str(item.discount_amount),
                    "line_total": str(item.line_total),
                }
                for item in self.items
            ],
            "summary": {
                "currency": self.summary.currency,
                "item_count": self.summary.item_count,
                "subtotal_gross": str(self.summary.subtotal_gross),
                "line_discounts": str(self.summary.line_discounts),
                "subtotal": str(self.summary.subtotal),
                "discount_type": self.summary.discount_type,
                "discount_value": str(self.summary.discount_value),
                "discount_amount": str(self.summary.discount_amount),
                "taxable_base": str(self.summary.taxable_base),
                "tax_label": self.summary.tax_label,
                "tax_rate_pct": format_decimal_compact(self.summary.tax_rate_pct),
                "tax_mode": self.summary.tax_mode,
                "tax_amount": str(self.summary.tax_amount),
                "grand_total": str(self.summary.grand_total),
                "formatted": {
                    "subtotal": format_money(self.summary.subtotal, self.summary.currency),
                    "discount_amount": format_money(self.summary.discount_amount, self.summary.currency),
                    "taxable_base": format_money(self.summary.taxable_base, self.summary.currency),
                    "tax_amount": format_money(self.summary.tax_amount, self.summary.currency),
                    "grand_total": format_money(self.summary.grand_total, self.summary.currency),
                },
            },
        }


def calculate_summary(items: Sequence[LineItem], config: InvoiceConfig) -> InvoiceSummary:
    """Compute exact Decimal invoice totals with ROUND_HALF_UP precision."""
    if not items:
        raise ValueError("At least one line item is required to generate an invoice.")

    subtotal_gross = round_money(sum((item.gross for item in items), ZERO))
    line_discounts = round_money(sum((item.discount_amount for item in items), ZERO))
    subtotal = round_money(sum((item.line_total for item in items), ZERO))

    if config.discount_type == "percent":
        discount_amount = round_money(subtotal * (config.discount_value / Decimal("100")))
    elif config.discount_type == "fixed":
        discount_amount = round_money(config.discount_value)
        if discount_amount > subtotal:
            raise ValueError(
                f"Fixed discount ({format_money(discount_amount, config.currency)}) "
                f"exceeds subtotal ({format_money(subtotal, config.currency)})."
            )
    else:
        discount_amount = ZERO

    after_discount = round_money(subtotal - discount_amount)

    if config.tax_mode == "exclusive":
        taxable_base = after_discount
        tax_amount = round_money(taxable_base * (config.tax_rate_pct / Decimal("100")))
        grand_total = round_money(taxable_base + tax_amount)
    else:
        grand_total = after_discount
        if config.tax_rate_pct == 0:
            taxable_base = grand_total
            tax_amount = ZERO
        else:
            divisor = Decimal("1") + (config.tax_rate_pct / Decimal("100"))
            taxable_base = round_money(grand_total / divisor)
            tax_amount = round_money(grand_total - taxable_base)

    return InvoiceSummary(
        currency=config.currency,
        item_count=len(items),
        subtotal_gross=subtotal_gross,
        line_discounts=line_discounts,
        subtotal=subtotal,
        discount_type=config.discount_type,
        discount_value=config.discount_value,
        discount_amount=discount_amount,
        taxable_base=taxable_base,
        tax_label=config.tax_label,
        tax_rate_pct=config.tax_rate_pct,
        tax_mode=config.tax_mode,
        tax_amount=tax_amount,
        grand_total=grand_total,
    )


def _normalize_row(raw_row: Dict[Any, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for k, v in raw_row.items():
        if k is None:
            continue
        key = str(k).strip().lower().replace(" ", "_").replace("-", "_")
        normalized[key] = v
    return normalized


def _pick(row: Dict[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            val = row[key]
            if isinstance(val, str):
                if val.strip() != "":
                    return val.strip()
            else:
                return val
    return default


def _parse_line_item_dict(raw: Dict[Any, Any], row_label: str) -> LineItem:
    if not isinstance(raw, dict):
        raise ValueError(f"{row_label}: item entry must be an object/row.")

    row = _normalize_row(raw)
    name = _pick(row, ("name", "item", "title", "service", "product"))
    description = _pick(row, ("description", "details", "notes"), default="")
    if name is None and description:
        name = description
        description = ""

    if not name:
        raise ValueError(f"{row_label}: missing required item name ('name' / 'item' / 'title').")

    qty_raw = _pick(row, ("qty", "quantity", "hours", "count"), default="1")
    unit_raw = _pick(row, ("unit", "uom"), default="")
    price_raw = _pick(row, ("unit_price", "price", "rate", "amount"))
    if price_raw is None:
        raise ValueError(
            f"{row_label}: missing required price ('unit_price' / 'price' / 'rate')."
        )

    disc_raw = _pick(row, ("discount_pct", "discount_percent", "discount"), default="0")
    if isinstance(disc_raw, str) and disc_raw.endswith("%"):
        disc_raw = disc_raw[:-1].strip()

    try:
        return LineItem(
            name=str(name),
            description=str(description or ""),
            qty=to_decimal(qty_raw, f"{row_label}.qty"),
            unit=str(unit_raw or ""),
            unit_price=to_decimal(price_raw, f"{row_label}.unit_price"),
            discount_pct=to_decimal(disc_raw, f"{row_label}.discount_pct"),
        )
    except ValueError as exc:
        raise ValueError(f"{row_label}: {exc}") from exc


def parse_csv_items(csv_text: str) -> List[LineItem]:
    """Parse line items from CSV text (handles UTF-8 BOM and ',' or ';' delimiters)."""
    cleaned = csv_text.lstrip("\ufeff")
    non_empty_lines = [line for line in cleaned.splitlines() if line.strip()]
    if not non_empty_lines:
        raise ValueError("CSV input is empty.")

    header_line = non_empty_lines[0]
    delimiter = ";" if (";" in header_line and "," not in header_line) else ","

    reader = csv.DictReader(io.StringIO(cleaned), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("CSV input is missing a header row.")

    items: List[LineItem] = []
    for idx, row in enumerate(reader, start=2):
        # Skip completely blank lines in CSV body
        if not any((str(v).strip() if v is not None else "") for v in row.values()):
            continue
        items.append(_parse_line_item_dict(row, f"CSV row {idx}"))

    if not items:
        raise ValueError("CSV input contains zero line items.")
    return items


def _parse_party_dict(raw: Any) -> PartyInfo:
    if not isinstance(raw, dict):
        return PartyInfo()
    norm = _normalize_row(raw)
    return PartyInfo(
        name=str(_pick(norm, ("name", "company", "organization"), default="") or ""),
        address=str(_pick(norm, ("address", "location"), default="") or ""),
        email=str(_pick(norm, ("email", "mail", "contact"), default="") or ""),
        tax_id=str(_pick(norm, ("tax_id", "vat_id", "tin", "inn"), default="") or ""),
    )


def parse_json_payload(json_text: str) -> Tuple[List[LineItem], Dict[str, Any]]:
    """Parse line items and optional document metadata from a JSON string."""
    cleaned = json_text.lstrip("\ufeff").strip()
    if not cleaned:
        raise ValueError("JSON input is empty.")

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON input: {exc}") from exc

    metadata: Dict[str, Any] = {}
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            raise ValueError("JSON object must contain an 'items' array.")
        metadata = data
    else:
        raise ValueError("JSON root must be a list of items or an object with 'items'.")

    items = [
        _parse_line_item_dict(entry, f"JSON item {idx}")
        for idx, entry in enumerate(raw_items, start=1)
    ]
    if not items:
        raise ValueError("JSON input contains zero line items.")

    return items, metadata


def detect_input_format(text: str, source_name: Optional[str] = None, fmt: str = "auto") -> str:
    """Detect whether input should be parsed as 'csv' or 'json'."""
    chosen = fmt.lower().strip()
    if chosen in ("csv", "json"):
        return chosen
    if chosen != "auto":
        raise ValueError(f"Unsupported input format '{fmt}'. Choose from auto, csv, json.")

    if source_name and source_name != "-":
        suffix = Path(source_name).suffix.lower()
        if suffix == ".json":
            return "json"
        if suffix == ".csv":
            return "csv"

    stripped = text.lstrip("\ufeff").lstrip()
    if stripped.startswith(("{", "[")):
        return "json"
    return "csv"


def load_invoice_input(
    text: str,
    source_name: Optional[str] = None,
    input_format: str = "auto",
) -> Tuple[List[LineItem], Dict[str, Any]]:
    """Load line items and optional metadata from CSV or JSON text."""
    detected = detect_input_format(text, source_name=source_name, fmt=input_format)
    if detected == "json":
        return parse_json_payload(text)
    return parse_csv_items(text), {}


def build_config_from_metadata(
    metadata: Dict[str, Any],
    overrides: Optional[Dict[str, Any]] = None,
) -> InvoiceConfig:
    """Build an InvoiceConfig by merging optional JSON metadata with explicit overrides."""
    ov = overrides or {}

    def pick_opt(key: str, default: Any = None, meta_keys: Sequence[str] = ()) -> Any:
        if key in ov and ov[key] is not None:
            return ov[key]
        for mk in (key, *meta_keys):
            if mk in metadata and metadata[mk] is not None:
                return metadata[mk]
        return default

    doc_type = str(pick_opt("doc_type", "invoice", ("type",)))
    doc_number = str(pick_opt("doc_number", "", ("number", "invoice_number", "quote_number")))
    issue_date = str(pick_opt("issue_date", "", ("date",)))
    due_date = str(pick_opt("due_date", "", ("valid_until",)))
    currency = str(pick_opt("currency", "USD"))
    tax_rate = pick_opt("tax_rate_pct", "0", ("tax_rate", "vat", "vat_rate"))
    tax_mode = str(pick_opt("tax_mode", "exclusive", ("vat_mode",)))
    tax_label = str(pick_opt("tax_label", "VAT"))
    notes = str(pick_opt("notes", "", ("terms",)))

    disc_spec = pick_opt("discount", None)
    disc_type_opt = str(pick_opt("discount_type", "auto"))
    if disc_spec is not None and str(disc_spec).strip() != "":
        discount_type, discount_value = parse_discount(disc_spec, discount_type=disc_type_opt)
    else:
        discount_type, discount_value = ("none", ZERO)

    meta_seller = _parse_party_dict(metadata.get("seller"))
    meta_client = _parse_party_dict(metadata.get("client"))

    seller = PartyInfo(
        name=ov.get("seller_name") if ov.get("seller_name") is not None else meta_seller.name,
        address=ov.get("seller_address") if ov.get("seller_address") is not None else meta_seller.address,
        email=ov.get("seller_email") if ov.get("seller_email") is not None else meta_seller.email,
        tax_id=ov.get("seller_tax_id") if ov.get("seller_tax_id") is not None else meta_seller.tax_id,
    )
    client = PartyInfo(
        name=ov.get("client_name") if ov.get("client_name") is not None else meta_client.name,
        address=ov.get("client_address") if ov.get("client_address") is not None else meta_client.address,
        email=ov.get("client_email") if ov.get("client_email") is not None else meta_client.email,
        tax_id=ov.get("client_tax_id") if ov.get("client_tax_id") is not None else meta_client.tax_id,
    )

    return InvoiceConfig(
        doc_type=doc_type,
        doc_number=doc_number,
        issue_date=issue_date,
        due_date=due_date,
        currency=currency,
        discount_type=discount_type,
        discount_value=discount_value,
        tax_rate_pct=to_decimal(tax_rate, "tax_rate"),
        tax_mode=tax_mode,
        tax_label=tax_label,
        seller=seller,
        client=client,
        notes=notes,
    )


def build_invoice_document(
    items: Sequence[LineItem],
    config: InvoiceConfig,
) -> InvoiceDocument:
    """Assemble and calculate a complete InvoiceDocument."""
    item_list = list(items)
    summary = calculate_summary(item_list, config)
    return InvoiceDocument(config=config, items=item_list, summary=summary)


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _esc_multiline(value: Any) -> str:
    escaped = _esc(value)
    return escaped.replace("\r\n", "\n").replace("\n", "<br>\n")


def _render_party_block(heading: str, party: PartyInfo, fallback_label: str) -> str:
    if not party.has_content():
        return (
            f'<div class="party-card">'
            f'<div class="eyebrow">{_esc(heading)}</div>'
            f'<div class="party-name muted">{_esc(fallback_label)}</div>'
            f"</div>"
        )

    parts = [f'<div class="party-card"><div class="eyebrow">{_esc(heading)}</div>']
    if party.name:
        parts.append(f'<div class="party-name">{_esc(party.name)}</div>')
    if party.tax_id:
        parts.append(f'<div class="party-meta">Tax ID: {_esc(party.tax_id)}</div>')
    if party.address:
        parts.append(f'<div class="party-meta">{_esc_multiline(party.address)}</div>')
    if party.email:
        parts.append(f'<div class="party-meta">{_esc(party.email)}</div>')
    parts.append("</div>")
    return "".join(parts)


def render_a4_html(doc: InvoiceDocument) -> str:
    """Render a print-ready, XSS-safe A4 editorial HTML invoice or quote."""
    cfg = doc.config
    summary = doc.summary
    currency = summary.currency

    is_quote = cfg.doc_type == "quote"
    doc_heading = "COMMERCIAL QUOTE" if is_quote else "INVOICE"
    due_label = "Valid Until" if is_quote else "Due Date"
    total_label = "Quoted Total" if is_quote else "Total Due"
    client_heading = "Prepared For" if is_quote else "Bill To"
    page_title = f"{doc_heading.title()} {cfg.doc_number}"

    has_line_discounts = any(item.discount_pct > 0 for item in doc.items)

    rows_html: List[str] = []
    for idx, item in enumerate(doc.items, start=1):
        qty_text = format_decimal_compact(item.qty)
        if item.unit:
            qty_text = f"{qty_text} {item.unit}"

        desc_html = f'<div class="item-title">{_esc(item.name)}</div>'
        if item.description:
            desc_html += f'<div class="item-desc">{_esc_multiline(item.description)}</div>'

        disc_cell = ""
        if has_line_discounts:
            disc_val = f"{format_decimal_compact(item.discount_pct)}%" if item.discount_pct > 0 else "—"
            disc_cell = f'<td class="num tabular">{_esc(disc_val)}</td>'

        rows_html.append(
            "<tr class=\"line-row\">"
            f'<td class="idx tabular">{idx}</td>'
            f"<td>{desc_html}</td>"
            f'<td class="num tabular">{_esc(qty_text)}</td>'
            f'<td class="num tabular">{_esc(format_money(item.unit_price, currency))}</td>'
            f"{disc_cell}"
            f'<td class="num strong tabular">{_esc(format_money(item.line_total, currency))}</td>'
            "</tr>"
        )

    disc_header = '<th class="num">Disc.</th>' if has_line_discounts else ""

    summary_rows: List[str] = [
        (
            '<div class="sum-row">'
            "<span>Subtotal</span>"
            f'<span class="tabular">{_esc(format_money(summary.subtotal, currency))}</span>'
            "</div>"
        )
    ]

    if summary.discount_amount > 0:
        if summary.discount_type == "percent":
            disc_label = f"Discount ({format_decimal_compact(summary.discount_value)}%)"
        else:
            disc_label = "Discount (Fixed)"
        summary_rows.append(
            '<div class="sum-row">'
            f"<span>{_esc(disc_label)}</span>"
            f'<span class="tabular">-{_esc(format_money(summary.discount_amount, currency))}</span>'
            "</div>"
        )

    if summary.discount_amount > 0 or summary.tax_rate_pct > 0:
        base_label = "Net Taxable Amount" if summary.tax_mode == "inclusive" else "Taxable Subtotal"
        summary_rows.append(
            '<div class="sum-row">'
            f"<span>{_esc(base_label)}</span>"
            f'<span class="tabular">{_esc(format_money(summary.taxable_base, currency))}</span>'
            "</div>"
        )

    if summary.tax_rate_pct > 0 or summary.tax_amount > 0:
        mode_note = " (incl.)" if summary.tax_mode == "inclusive" else ""
        tax_title = f"{summary.tax_label} ({format_decimal_compact(summary.tax_rate_pct)}%{mode_note})"
        summary_rows.append(
            '<div class="sum-row">'
            f"<span>{_esc(tax_title)}</span>"
            f'<span class="tabular">{_esc(format_money(summary.tax_amount, currency))}</span>'
            "</div>"
        )

    due_meta_html = ""
    if cfg.due_date:
        due_meta_html = (
            '<div class="meta-item">'
            f'<span class="meta-label">{_esc(due_label)}</span>'
            f'<span class="meta-value tabular">{_esc(cfg.due_date)}</span>'
            "</div>"
        )

    notes_html = ""
    if cfg.notes.strip():
        notes_html = (
            '<section class="notes-box">'
            '<div class="eyebrow">Notes &amp; Terms</div>'
            f'<div class="notes-body">{_esc_multiline(cfg.notes)}</div>'
            "</section>"
        )

    seller_block = _render_party_block("From", cfg.seller, "Issuer details not provided")
    client_block = _render_party_block(client_heading, cfg.client, "Recipient details not provided")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(page_title)}</title>
<style>
  @page {{
    size: A4;
    margin: 14mm;
  }}

  :root {{
    --ink: #111827;
    --ink-soft: #4b5563;
    --ink-muted: #6b7280;
    --border: #e5e7eb;
    --border-strong: #d1d5db;
    --surface: #f8fafc;
    --accent: #0f172a;
    --canvas: #f3f4f6;
  }}

  * {{
    box-sizing: border-box;
  }}

  html, body {{
    margin: 0;
    padding: 0;
    color: var(--ink);
    background: var(--canvas);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }}

  .tabular {{
    font-variant-numeric: tabular-nums;
    font-feature-settings: "tnum" 1;
    white-space: nowrap;
  }}

  .sheet {{
    width: 210mm;
    min-height: 297mm;
    margin: 24px auto;
    padding: 16mm 16mm 14mm;
    background: #ffffff;
    border: 1px solid var(--border);
    box-shadow: 0 10px 30px rgba(15, 23, 42, 0.06);
    display: flex;
    flex-direction: column;
  }}

  .doc-header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 24px;
    padding-bottom: 18px;
    border-bottom: 2px solid var(--accent);
  }}

  .doc-badge {{
    display: inline-block;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-muted);
    margin-bottom: 4px;
  }}

  .doc-title {{
    margin: 0;
    font-size: 26px;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: var(--accent);
    line-height: 1.15;
  }}

  .doc-subtitle {{
    margin-top: 4px;
    font-size: 13px;
    color: var(--ink-soft);
  }}

  .meta-grid {{
    display: grid;
    grid-template-columns: repeat(2, auto);
    gap: 8px 20px;
    text-align: right;
  }}

  .meta-item {{
    display: flex;
    flex-direction: column;
  }}

  .meta-label {{
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-muted);
  }}

  .meta-value {{
    font-size: 13px;
    font-weight: 600;
    color: var(--ink);
  }}

  .parties {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
    margin-top: 20px;
    padding-bottom: 18px;
    border-bottom: 1px solid var(--border);
  }}

  .eyebrow {{
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--ink-muted);
    margin-bottom: 6px;
  }}

  .party-name {{
    font-size: 15px;
    font-weight: 600;
    color: var(--ink);
    margin-bottom: 4px;
  }}

  .party-name.muted {{
    font-weight: 400;
    color: var(--ink-muted);
    font-size: 13px;
  }}

  .party-meta {{
    font-size: 12.5px;
    color: var(--ink-soft);
    line-height: 1.45;
  }}

  .items-table {{
    width: 100%;
    border-collapse: collapse;
    margin-top: 20px;
  }}

  .items-table th {{
    padding: 9px 10px;
    font-size: 10.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: var(--ink-soft);
    background: var(--surface);
    border-top: 1px solid var(--border-strong);
    border-bottom: 1px solid var(--border-strong);
    text-align: left;
  }}

  .items-table td {{
    padding: 10px;
    vertical-align: top;
    border-bottom: 1px solid var(--border);
  }}

  .items-table th.num,
  .items-table td.num {{
    text-align: right;
  }}

  .items-table th.idx,
  .items-table td.idx {{
    width: 34px;
    color: var(--ink-muted);
  }}

  .item-title {{
    font-weight: 600;
    color: var(--ink);
  }}

  .item-desc {{
    margin-top: 2px;
    font-size: 12px;
    color: var(--ink-soft);
  }}

  .strong {{
    font-weight: 600;
  }}

  .line-row {{
    break-inside: avoid;
    page-break-inside: avoid;
  }}

  .summary-wrap {{
    display: flex;
    justify-content: flex-end;
    margin-top: 20px;
    break-inside: avoid;
    page-break-inside: avoid;
  }}

  .summary-card {{
    width: 310px;
    border: 1px solid var(--border);
    background: var(--surface);
    border-radius: 4px;
    padding: 12px 16px;
  }}

  .sum-row {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
    padding: 4px 0;
    font-size: 12.5px;
    color: var(--ink-soft);
  }}

  .sum-total {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
    margin-top: 8px;
    padding-top: 10px;
    border-top: 2px solid var(--accent);
    font-size: 15px;
    font-weight: 700;
    color: var(--accent);
  }}

  .notes-box {{
    margin-top: 22px;
    padding: 12px 14px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: #ffffff;
    break-inside: avoid;
    page-break-inside: avoid;
  }}

  .notes-body {{
    font-size: 12.5px;
    color: var(--ink-soft);
    line-height: 1.5;
  }}

  .doc-footer {{
    margin-top: auto;
    padding-top: 24px;
    border-top: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    font-size: 11px;
    color: var(--ink-muted);
    break-inside: avoid;
    page-break-inside: avoid;
  }}

  @media print {{
    html, body {{
      background: #ffffff;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}

    .sheet {{
      width: auto;
      min-height: 0;
      margin: 0;
      padding: 0;
      border: none;
      box-shadow: none;
    }}
  }}
</style>
</head>
<body>
  <main class="sheet">
    <header class="doc-header">
      <div>
        <div class="doc-badge">{_esc(doc_heading)}</div>
        <h1 class="doc-title">{_esc(cfg.doc_number)}</h1>
        <div class="doc-subtitle">{_esc(summary.item_count)} line item{"s" if summary.item_count != 1 else ""} &middot; Currency: {_esc(currency)}</div>
      </div>
      <div class="meta-grid">
        <div class="meta-item">
          <span class="meta-label">Issue Date</span>
          <span class="meta-value tabular">{_esc(cfg.issue_date)}</span>
        </div>
        {due_meta_html}
      </div>
    </header>

    <section class="parties">
      {seller_block}
      {client_block}
    </section>

    <table class="items-table">
      <thead>
        <tr>
          <th class="idx">#</th>
          <th>Description</th>
          <th class="num">Qty</th>
          <th class="num">Unit Price</th>
          {disc_header}
          <th class="num">Amount</th>
        </tr>
      </thead>
      <tbody>
        {"".join(rows_html)}
      </tbody>
    </table>

    <section class="summary-wrap">
      <div class="summary-card">
        {"".join(summary_rows)}
        <div class="sum-total">
          <span>{_esc(total_label)} ({_esc(currency)})</span>
          <span class="tabular">{_esc(format_money(summary.grand_total, currency))}</span>
        </div>
      </div>
    </section>

    {notes_html}

    <footer class="doc-footer">
      <span>Document: {_esc(cfg.doc_number)}</span>
      <span>Currency: {_esc(currency)}</span>
    </footer>
  </main>
</body>
</html>
"""
