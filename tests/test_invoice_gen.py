import contextlib
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cli
from invoice_gen import (
    InvoiceConfig,
    LineItem,
    PartyInfo,
    build_config_from_metadata,
    build_invoice_document,
    calculate_summary,
    format_money,
    load_invoice_input,
    parse_csv_items,
    parse_discount,
    parse_json_payload,
    render_a4_html,
    round_money,
)


class TestDecimalMathAndRounding(unittest.TestCase):
    def test_round_half_up_midpoint_behavior(self) -> None:
        # ROUND_HALF_UP rounds exact .xx5 away from zero (unlike ROUND_HALF_EVEN)
        self.assertEqual(round_money(Decimal("0.005")), Decimal("0.01"))
        self.assertEqual(round_money(Decimal("0.015")), Decimal("0.02"))
        self.assertEqual(round_money(Decimal("0.025")), Decimal("0.03"))
        self.assertEqual(round_money(Decimal("10.0049")), Decimal("10.00"))
        self.assertEqual(round_money(Decimal("-0.00")), Decimal("0.00"))

    def test_line_item_gross_discount_and_net_rounding(self) -> None:
        item = LineItem(
            name="Precision Engineering",
            qty=Decimal("0.5"),
            unit_price=Decimal("0.05"),
            discount_pct=Decimal("10"),
        )
        # 0.5 * 0.05 = 0.025 -> ROUND_HALF_UP => 0.03
        self.assertEqual(item.gross, Decimal("0.03"))
        # 10% of 0.03 = 0.003 -> 0.00
        self.assertEqual(item.discount_amount, Decimal("0.00"))
        self.assertEqual(item.line_total, Decimal("0.03"))

        item2 = LineItem(
            name="Design Sprint",
            qty=Decimal("3"),
            unit_price=Decimal("111.11"),
            discount_pct=Decimal("15"),
        )
        # gross = 333.33; 15% = 49.9995 -> 50.00; line_total = 283.33
        self.assertEqual(item2.gross, Decimal("333.33"))
        self.assertEqual(item2.discount_amount, Decimal("50.00"))
        self.assertEqual(item2.line_total, Decimal("283.33"))

    def test_percent_discount_and_exclusive_vat(self) -> None:
        items = [
            LineItem(name="API Build", qty=Decimal("10"), unit_price=Decimal("100.00"), discount_pct=Decimal("10")),
            LineItem(name="Cloud Setup", qty=Decimal("1"), unit_price=Decimal("300.00")),
        ]
        # Line totals: 900.00 + 300.00 = 1200.00
        cfg = InvoiceConfig(
            currency="USD",
            discount_type="percent",
            discount_value=Decimal("10"),
            tax_rate_pct=Decimal("20"),
            tax_mode="exclusive",
        )
        summary = calculate_summary(items, cfg)
        self.assertEqual(summary.subtotal_gross, Decimal("1300.00"))
        self.assertEqual(summary.line_discounts, Decimal("100.00"))
        self.assertEqual(summary.subtotal, Decimal("1200.00"))
        self.assertEqual(summary.discount_amount, Decimal("120.00"))
        self.assertEqual(summary.taxable_base, Decimal("1080.00"))
        self.assertEqual(summary.tax_amount, Decimal("216.00"))
        self.assertEqual(summary.grand_total, Decimal("1296.00"))

    def test_fixed_discount_and_fractional_vat_rounding(self) -> None:
        items = [LineItem(name="Audit", qty=Decimal("1"), unit_price=Decimal("83.33"))]
        cfg = InvoiceConfig(
            currency="USD",
            discount_type="fixed",
            discount_value=Decimal("50.00"),
            tax_rate_pct=Decimal("7.5"),
            tax_mode="exclusive",
        )
        summary = calculate_summary(items, cfg)
        # taxable = 33.33; 7.5% of 33.33 = 2.49975 -> 2.50; grand_total = 35.83
        self.assertEqual(summary.subtotal, Decimal("83.33"))
        self.assertEqual(summary.discount_amount, Decimal("50.00"))
        self.assertEqual(summary.taxable_base, Decimal("33.33"))
        self.assertEqual(summary.tax_amount, Decimal("2.50"))
        self.assertEqual(summary.grand_total, Decimal("35.83"))

    def test_fixed_discount_exceeding_subtotal_raises(self) -> None:
        items = [LineItem(name="Small Task", qty=Decimal("1"), unit_price=Decimal("40.00"))]
        cfg = InvoiceConfig(currency="USD", discount_type="fixed", discount_value=Decimal("50.00"))
        with self.assertRaises(ValueError):
            calculate_summary(items, cfg)

    def test_inclusive_vat_extraction(self) -> None:
        items = [LineItem(name="Annual License", qty=Decimal("1"), unit_price=Decimal("1200.00"))]
        cfg = InvoiceConfig(
            currency="EUR",
            tax_rate_pct=Decimal("20"),
            tax_mode="inclusive",
        )
        summary = calculate_summary(items, cfg)
        self.assertEqual(summary.taxable_base, Decimal("1000.00"))
        self.assertEqual(summary.tax_amount, Decimal("200.00"))
        self.assertEqual(summary.grand_total, Decimal("1200.00"))
        self.assertEqual(summary.taxable_base + summary.tax_amount, summary.grand_total)


class TestCurrencyAndDiscountParsing(unittest.TestCase):
    def test_multi_currency_formatting(self) -> None:
        amount = Decimal("1234.50")
        self.assertEqual(format_money(amount, "USD"), "$1,234.50")
        self.assertEqual(format_money(amount, "EUR"), "€1,234.50")
        self.assertEqual(format_money(amount, "GBP"), "£1,234.50")
        self.assertEqual(format_money(amount, "RUB"), "1 234.50 ₽")
        self.assertEqual(format_money(Decimal("-45.5"), "USD"), "-$45.50")
        self.assertEqual(format_money(Decimal("-1500"), "RUB"), "-1 500.00 ₽")

        with self.assertRaises(ValueError):
            format_money(amount, "JPY")

    def test_parse_discount_variants(self) -> None:
        self.assertEqual(parse_discount("15%"), ("percent", Decimal("15")))
        self.assertEqual(parse_discount("75.5"), ("fixed", Decimal("75.50")))
        self.assertEqual(parse_discount("12", discount_type="percent"), ("percent", Decimal("12")))
        self.assertEqual(parse_discount("0"), ("none", Decimal("0.00")))
        self.assertEqual(parse_discount("0%"), ("none", Decimal("0.00")))

        with self.assertRaises(ValueError):
            parse_discount("110%")
        with self.assertRaises(ValueError):
            parse_discount("-5")
        with self.assertRaises(ValueError):
            parse_discount("10%", discount_type="fixed")


class TestCsvAndJsonParsing(unittest.TestCase):
    def test_csv_with_bom_and_column_aliases(self) -> None:
        csv_data = (
            "\ufeffItem,Details,Quantity,Price,Discount\n"
            "Consulting,Architecture review,2.5,120.00,10%\n"
            "Support,,1,50,0\n"
        )
        items = parse_csv_items(csv_data)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].name, "Consulting")
        self.assertEqual(items[0].description, "Architecture review")
        self.assertEqual(items[0].qty, Decimal("2.5"))
        self.assertEqual(items[0].unit_price, Decimal("120.00"))
        self.assertEqual(items[0].discount_pct, Decimal("10"))
        self.assertEqual(items[0].line_total, Decimal("270.00"))
        self.assertEqual(items[1].line_total, Decimal("50.00"))

    def test_csv_semicolon_delimiter_and_blank_lines(self) -> None:
        csv_data = (
            "name;qty;unit_price\n"
            "Hosting;3;25.00\n"
            "\n"
            "Domain;1;15.00\n"
        )
        items = parse_csv_items(csv_data)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].line_total, Decimal("75.00"))
        self.assertEqual(items[1].line_total, Decimal("15.00"))

    def test_csv_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            parse_csv_items("")
        with self.assertRaises(ValueError):
            parse_csv_items("name,qty\nWork,2\n")
        with self.assertRaises(ValueError):
            parse_csv_items("name,qty,unit_price\nWork,-1,50\n")

    def test_json_list_and_object_with_metadata_overrides(self) -> None:
        list_json = json.dumps([{"name": "Setup", "qty": 2, "unit_price": "75.50"}])
        items, meta = parse_json_payload(list_json)
        self.assertEqual(len(items), 1)
        self.assertEqual(meta, {})
        self.assertEqual(items[0].line_total, Decimal("151.00"))

        obj_json = json.dumps({
            "doc_type": "quote",
            "doc_number": "QUO-900",
            "currency": "GBP",
            "tax_rate": "20",
            "discount": "10%",
            "seller": {"name": "Seller Co"},
            "client": {"name": "Buyer Co"},
            "items": [{"name": "Retainer", "qty": 1, "unit_price": "500.00"}],
        })
        items2, meta2 = parse_json_payload(obj_json)
        cfg = build_config_from_metadata(meta2, overrides={"currency": "EUR", "doc_number": "QUO-901"})
        self.assertEqual(cfg.doc_type, "quote")
        self.assertEqual(cfg.currency, "EUR")
        self.assertEqual(cfg.doc_number, "QUO-901")
        self.assertEqual(cfg.seller.name, "Seller Co")
        self.assertEqual(cfg.client.name, "Buyer Co")
        self.assertEqual(len(items2), 1)

    def test_json_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            parse_json_payload("{bad json")
        with self.assertRaises(ValueError):
            parse_json_payload('{"not_items": []}')
        with self.assertRaises(ValueError):
            parse_json_payload("[]")


class TestHtmlRenderingAndXssEscaping(unittest.TestCase):
    def test_html_contains_a4_css_tabular_nums_and_quote_labels(self) -> None:
        items = [LineItem(name="UI Design", qty=Decimal("5"), unit="hrs", unit_price=Decimal("80.00"))]
        cfg = InvoiceConfig(
            doc_type="quote",
            doc_number="QUO-2026-07",
            issue_date="2026-03-10",
            due_date="2026-04-10",
            currency="GBP",
            tax_rate_pct=Decimal("20"),
            seller=PartyInfo(name="Studio North"),
            client=PartyInfo(name="Acme Ltd"),
        )
        doc = build_invoice_document(items, cfg)
        rendered = render_a4_html(doc)

        self.assertIn("@page", rendered)
        self.assertIn("size: A4;", rendered)
        self.assertIn("margin: 14mm;", rendered)
        self.assertIn("font-variant-numeric: tabular-nums;", rendered)
        self.assertIn("COMMERCIAL QUOTE", rendered)
        self.assertIn("Quoted Total (GBP)", rendered)
        self.assertIn("Valid Until", rendered)
        self.assertIn("£400.00", rendered)
        self.assertIn("£480.00", rendered)

    def test_html_xss_escaping_across_all_fields(self) -> None:
        xss_script = "<script>alert('xss')</script>"
        xss_img = '<img src=x onerror="alert(1)">'
        xss_attr = '" onmouseover="alert(2)'

        items = [
            LineItem(
                name=f"Service {xss_script}",
                description=f"Details {xss_img}",
                qty=Decimal("1"),
                unit=xss_attr,
                unit_price=Decimal("100.00"),
            )
        ]
        cfg = InvoiceConfig(
            doc_number=f"INV-{xss_script}",
            issue_date="2026-03-01",
            due_date=xss_img,
            currency="USD",
            tax_label=f"VAT{xss_script}",
            tax_rate_pct=Decimal("10"),
            seller=PartyInfo(name=xss_script, address=xss_img, email=xss_attr, tax_id=xss_script),
            client=PartyInfo(name=xss_img, address=xss_script, email=xss_attr, tax_id=xss_img),
            notes=f"Line 1 {xss_script}\nLine 2 {xss_img}",
        )
        doc = build_invoice_document(items, cfg)
        rendered = render_a4_html(doc)

        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<img src=x", rendered)
        self.assertNotIn('" onmouseover="alert(2)', rendered)
        self.assertIn("&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;", rendered)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", rendered)
        self.assertIn("&quot; onmouseover=&quot;alert(2)", rendered)


class TestCliExecutionAndSamples(unittest.TestCase):
    def test_cli_generates_html_file_from_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            csv_file = tmp / "items.csv"
            html_file = tmp / "out" / "invoice.html"
            csv_file.write_text(
                "name,qty,unit_price\nBackend Work,4,150.00\nQA Pass,2,50.00\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main([
                    str(csv_file),
                    "-o",
                    str(html_file),
                    "--currency",
                    "USD",
                    "--discount",
                    "10%",
                    "--vat",
                    "20",
                    "--number",
                    "INV-555",
                    "--seller-name",
                    "Northwind LLC",
                    "--client-name",
                    "Beta Corp",
                ])

            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn("INV-555", stdout.getvalue())
            self.assertIn("$756.00", stdout.getvalue())
            self.assertTrue(html_file.is_file())
            html_content = html_file.read_text(encoding="utf-8")
            self.assertIn("size: A4;", html_content)
            self.assertIn("Northwind LLC", html_content)
            self.assertIn("Beta Corp", html_content)
            self.assertIn("$756.00", html_content)

    def test_cli_json_output_and_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_file = tmp / "items.json"
            json_file.write_text(
                json.dumps([{"name": "Package", "qty": 1, "unit_price": "100.00"}]),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main([
                    str(json_file),
                    "--format",
                    "json",
                    "--currency",
                    "RUB",
                    "--vat",
                    "20",
                ])
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["summary"]["grand_total"], "120.00")
            self.assertEqual(payload["summary"]["formatted"]["grand_total"], "120.00 ₽")

            # Missing file -> exit code 2
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                missing_code = cli.main([str(tmp / "missing.csv")])
            self.assertEqual(missing_code, 2)

            # Fixed discount > subtotal -> exit code 1
            err_buf2 = io.StringIO()
            with contextlib.redirect_stderr(err_buf2):
                bad_disc_code = cli.main([str(json_file), "--discount", "500.00"])
            self.assertEqual(bad_disc_code, 1)
            self.assertIn("exceeds subtotal", err_buf2.getvalue())

    def test_bundled_examples_parse_and_render(self) -> None:
        csv_sample = PROJECT_ROOT / "examples" / "sample_items.csv"
        json_sample = PROJECT_ROOT / "examples" / "sample_invoice.json"

        csv_items, _ = load_invoice_input(csv_sample.read_text(encoding="utf-8"), str(csv_sample))
        self.assertGreaterEqual(len(csv_items), 3)
        csv_doc = build_invoice_document(csv_items, InvoiceConfig(currency="USD", tax_rate_pct=Decimal("20")))
        self.assertIn("INVOICE", render_a4_html(csv_doc))

        json_items, json_meta = load_invoice_input(json_sample.read_text(encoding="utf-8"), str(json_sample))
        json_cfg = build_config_from_metadata(json_meta)
        json_doc = build_invoice_document(json_items, json_cfg)
        rendered_json = render_a4_html(json_doc)
        self.assertIn("INV-2026-042", rendered_json)
        self.assertIn("€", rendered_json)


if __name__ == "__main__":
    unittest.main()
