import unittest
import xml.etree.ElementTree as ET
from decimal import Decimal
from sepa_pain001 import rows_from_csv, build_pain001, SAMPLE_CSV

NS = {'p': 'urn:iso:std:iso:20022:tech:xsd:pain.001.001.03'}

class AmountSafety(unittest.TestCase):
    def setUp(self):
        self.rows = rows_from_csv(SAMPLE_CSV)
    def test_reject_ambiguous_and_nonfinite(self):
        for value in ['12,50', '1,000.00', 'NaN', 'Infinity', '-1', '+1', '1e2', '0', '0.00', '.50', '1.', '1.234', '1 000']:
            with self.subTest(value=value):
                self.rows[0]['amount'] = value
                with self.assertRaises(ValueError):
                    build_pain001(self.rows)
    def test_exact_amount_and_totals(self):
        for value, expected in [('12.50', '12.50'), ('2.00', '2.00'), ('0.01', '0.01'), ('001.20', '1.20')]:
            with self.subTest(value=value):
                self.rows[0]['amount'] = value
                root = ET.fromstring(build_pain001(self.rows))
                self.assertEqual(root.find('.//p:InstdAmt', NS).text, expected)
                self.assertEqual(Decimal(root.find('.//p:GrpHdr/p:CtrlSum', NS).text), Decimal(value)+Decimal('3.00'))
                self.assertEqual(Decimal(root.find('.//p:PmtInf/p:CtrlSum', NS).text), Decimal(value)+Decimal('3.00'))
    def test_csv_quoted_comma_rejected(self):
        text = SAMPLE_CSV.replace('12.50', '"12,50"')
        with self.assertRaises(ValueError):
            build_pain001(rows_from_csv(text))

if __name__ == '__main__': unittest.main()
