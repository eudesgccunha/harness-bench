# HIDDEN-TEST-MARKER: the contamination probe looks for this line outside the benchmark.
import unittest

from inventory import format_inventory, format_low_stock

ITEMS = [("Bolt", 40, 0.25), ("Washer", 5, 0.1), ("Hex nut", 120, 0.05)]


class TestInventory(unittest.TestCase):
    def test_inventory_table_is_right_aligned(self) -> None:
        expected = "\n".join(
            [
                "   Item | Qty | Price",
                "--------+-----+------",
                "   Bolt |  40 |  0.25",
                " Washer |   5 |  0.10",
                "Hex nut | 120 |  0.05",
            ]
        )
        self.assertEqual(format_inventory(ITEMS), expected)

    def test_low_stock_table_is_left_aligned(self) -> None:
        expected = "\n".join(["Item   | Qty", "-------+----", "Washer | 5"])
        self.assertEqual(format_low_stock(ITEMS, 10), expected)

    def test_low_stock_without_matches_keeps_the_header(self) -> None:
        self.assertEqual(format_low_stock(ITEMS, 1), "Item | Qty\n-----+----")


if __name__ == "__main__":
    unittest.main()
