import unittest
from decimal import Decimal

from invoice import Order, render_invoice

ITEMS = [("Pen", 3, Decimal("1.50")), ("Book", 1, Decimal("12.00"))]


class TestInvoice(unittest.TestCase):
    def test_invoice_without_a_discount(self) -> None:
        expected = "Pen x3: 4.50\nBook x1: 12.00\nSubtotal: 16.50\nTax: 1.32\nTotal: 17.82"
        self.assertEqual(render_invoice(Order(ITEMS)), expected)

    def test_percentage_discount(self) -> None:
        expected = "Pen x3: 4.50\nBook x1: 12.00\nSubtotal: 16.50\nDiscount: -1.65\nTax: 1.19\nTotal: 16.04"
        self.assertEqual(render_invoice(Order(ITEMS, "SAVE10")), expected)

    def test_flat_discount_never_goes_below_zero(self) -> None:
        expected = "Gum x1: 2.00\nSubtotal: 2.00\nDiscount: -2.00\nTax: 0.00\nTotal: 0.00"
        self.assertEqual(render_invoice(Order([("Gum", 1, Decimal("2.00"))], "FLAT5")), expected)


if __name__ == "__main__":
    unittest.main()
