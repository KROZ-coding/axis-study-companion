import json
import unittest

from app.services.billing import Price, PriceRegistry, PriceUnavailable


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.price = Price("provider", "gpt-5.6-terra", "configured-v1", "site-credit", 1_000_000, 2_000_000)

    def test_explicit_integer_units_and_missing_usage(self):
        self.assertEqual(self.price.cost({"prompt_tokens": 7, "completion_tokens": 3}), 13)
        self.assertIsNone(self.price.cost(None))
        self.assertIsNone(self.price.cost({"prompt_tokens": 2}))
        self.assertIsNone(self.price.cost({"prompt_tokens": True, "completion_tokens": 2}))
        self.assertGreaterEqual(self.price.reserve([{"role": "user", "content": "数学"}], 10), 20)

    def test_rounds_up_fractional_cost_and_requires_explicit_price(self):
        fractional = Price("p", "m", "v1", "credit", 1, 1)
        self.assertEqual(fractional.cost({"prompt_tokens": 1, "completion_tokens": 0}), 1)
        with self.assertRaises(PriceUnavailable):
            PriceRegistry().get("p", "m")
        registry = PriceRegistry(json.dumps([self.price.snapshot()]))
        self.assertEqual(registry.get("provider", "gpt-5.6-terra"), self.price)
        with self.assertRaises(PriceUnavailable):
            PriceRegistry(json.dumps([self.price.snapshot(), self.price.snapshot()]))
        with self.assertRaises(PriceUnavailable):
            PriceRegistry('[{"input_units_per_million":NaN}]')


if __name__ == "__main__":
    unittest.main()
