import unittest
from copy import deepcopy
from src.paper_settlement import validate_settlement


class SettlementResponseTests(unittest.TestCase):
    def empty(self):
        return {'settled': 0, 'pending_or_skipped': 0, 'rejected': 0, 'results': [], 'virtual_only': True}

    def test_empty_batch(self):
        value = self.empty()
        self.assertEqual(validate_settlement(value), value)

    def test_each_supported_status(self):
        for status, key in [('settled', 'settled'), ('rejected', 'rejected'), ('pending', 'pending_or_skipped'), ('skipped', 'pending_or_skipped'), ('already_settled', 'pending_or_skipped')]:
            with self.subTest(status=status):
                value = self.empty()
                value[key] = 1
                value['results'] = [{'status': status}]
                self.assertEqual(validate_settlement(value), value)

    def test_non_dictionary(self):
        for value in (None, [], 'settled'):
            with self.assertRaises(ValueError):
                validate_settlement(value)

    def test_virtual_flag_required(self):
        value = self.empty()
        value['virtual_only'] = False
        with self.assertRaises(ValueError):
            validate_settlement(value)

    def test_boolean_is_not_a_count(self):
        value = self.empty()
        value['settled'] = False
        with self.assertRaises(ValueError):
            validate_settlement(value)

    def test_negative_count(self):
        value = self.empty()
        value['settled'] = -1
        with self.assertRaises(ValueError):
            validate_settlement(value)

    def test_missing_count(self):
        value = self.empty()
        del value['rejected']
        with self.assertRaises(ValueError):
            validate_settlement(value)

    def test_inconsistent_total(self):
        value = self.empty()
        value['settled'] = 1
        with self.assertRaises(ValueError):
            validate_settlement(value)

    def test_inconsistent_categories(self):
        value = self.empty()
        value['settled'] = 1
        value['results'] = [{'status': 'pending'}]
        with self.assertRaises(ValueError):
            validate_settlement(value)

    def test_unknown_status(self):
        value = self.empty()
        value['pending_or_skipped'] = 1
        value['results'] = [{'status': 'unknown'}]
        with self.assertRaises(ValueError):
            validate_settlement(value)

    def test_invalid_row(self):
        value = self.empty()
        value['settled'] = 1
        value['results'] = [None]
        with self.assertRaises(ValueError):
            validate_settlement(value)

    def test_batch_limit(self):
        value = self.empty()
        value['settled'] = 51
        value['results'] = [{'status': 'settled'}] * 51
        with self.assertRaises(ValueError):
            validate_settlement(value)


if __name__ == '__main__':
    unittest.main()
