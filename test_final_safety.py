import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from sepa_pain001 import SAMPLE_CSV, rows_from_csv, build_pain001

class FinalSafety(unittest.TestCase):
    def setUp(self):
        self.rows = rows_from_csv(SAMPLE_CSV)

    def test_balanced_width_decimal_comma(self):
        text = ('debtor_name,debtor_iban,creditor_name,creditor_iban,amount,currency,remittance\n'
                'A,DE89370400440532013000,B,NL91ABNA0417164300,12,50,EUR\n')
        with self.assertRaisesRegex(ValueError, 'exactly 2 decimals'):
            build_pain001(rows_from_csv(text))

    def test_acknowledgement_per_cli_run(self):
        for args, code in (([], 2), (['--reviewed-input'], 0)):
            result = subprocess.run([sys.executable, 'sepa_pain001.py', '-', *args], input=SAMPLE_CSV,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, code, result.stderr)
            if code: self.assertIn('--reviewed-input', result.stderr)
            else: ET.fromstring(result.stdout)

    def test_dates(self):
        for value in ('2026-02-29', '2026-13-01', '2026-2-01', '0000-01-01'):
            self.rows[0]['execution_date'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'execution_date'):
                build_pain001(self.rows)
        self.rows[0]['execution_date'] = '2028-02-29'
        ET.fromstring(build_pain001(self.rows))

    def test_e2e_no_silent_changes(self):
        for value in ('😀', 'abc😀', 'a' * 36, '   ', 'a\t'):
            self.rows[0]['end_to_end_id'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'end_to_end_id'):
                build_pain001(self.rows)
        self.rows[0]['end_to_end_id'] = ' ID 123 '
        self.assertIn('<EndToEndId> ID 123 </EndToEndId>', build_pain001(self.rows))

    def test_illegal_controls_everywhere(self):
        for field in ('debtor_name', 'creditor_name', 'remittance', 'end_to_end_id', 'execution_date', 'currency'):
            self.rows[0][field] = '\x01'
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'illegal XML'):
                build_pain001(self.rows)
            self.rows = rows_from_csv(SAMPLE_CSV)
        for kwargs in ({'initiating_party': 'A\x00'}, {'msg_id': 'M\x0b'}):
            with self.assertRaisesRegex(ValueError, 'illegal XML'):
                build_pain001(self.rows, **kwargs)
        with self.assertRaisesRegex(ValueError, 'illegal XML'):
            rows_from_csv(SAMPLE_CSV.replace('Invoice', '\x01Invoice'))

    def test_metadata_lengths_and_groups(self):
        for kwargs in ({'initiating_party': 'x' * 71}, {'msg_id': 'x' * 36}, {'msg_id': ''}):
            with self.assertRaises(ValueError): build_pain001(self.rows, **kwargs)
        for field, length in (('debtor_name', 71), ('creditor_name', 71), ('remittance', 141)):
            self.rows[0][field] = 'x' * length
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'exceeds'):
                build_pain001(self.rows)
            self.rows = rows_from_csv(SAMPLE_CSV)
        for field, value in (('debtor_name', 'Different'), ('debtor_bic', ''), ('debtor_bic', 'ABNANL2A')):
            self.rows[1][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, 'conflicting debtor'):
                build_pain001(self.rows)
            self.rows = rows_from_csv(SAMPLE_CSV)
        self.rows[1]['debtor_bic'] = ' cobadeffxxx '
        ET.fromstring(build_pain001(self.rows))

if __name__ == '__main__': unittest.main()
