# HIDDEN-TEST-MARKER: the contamination probe looks for this line outside the benchmark.
import unittest
from decimal import Decimal
from unittest import mock

import invoice
from invoice import Order, discount_amount, render_invoice


class TestDiscountAmount(unittest.TestCase):
    def test_no_code_and_unknown_codes_give_no_discount(self) -> None:
        self.assertEqual(discount_amount(Decimal("10.00"), None), Decimal("0"))
        self.assertEqual(discount_amount(Decimal("10.00"), "NOPE"), Decimal("0"))

    def test_save10_is_ten_percent_rounded_half_up_to_the_cent(self) -> None:
        self.assertEqual(discount_amount(Decimal("16.50"), "SAVE10"), Decimal("1.65"))
        self.assertEqual(discount_amount(Decimal("0.25"), "SAVE10"), Decimal("0.03"))

    def test_flat5_is_five_but_never_more_than_the_subtotal(self) -> None:
        self.assertEqual(discount_amount(Decimal("20.00"), "FLAT5"), Decimal("5.00"))
        self.assertEqual(discount_amount(Decimal("2.00"), "FLAT5"), Decimal("2.00"))


class TestRenderUsesIt(unittest.TestCase):
    def test_render_invoice_takes_the_discount_from_discount_amount(self) -> None:
        order = Order([("Pen", 2, Decimal("5.00"))], "SAVE10")
        with mock.patch.object(invoice, "discount_amount", return_value=Decimal("3.00")) as patched:
            text = render_invoice(order)
        patched.assert_called_once_with(Decimal("10.00"), "SAVE10")
        self.assertIn("Discount: -3.00", text)
        self.assertIn("Total: 7.56", text)


if __name__ == "__main__":
    unittest.main()
