import csv
import io
import unittest
import xml.etree.ElementTree as ET
from decimal import localcontext

from sepa_pain001 import SAMPLE_CSV, rows_from_csv, build_pain001

NS = {'p': 'urn:iso:std:iso:20022:tech:xsd:pain.001.001.03'}
HEAD = 'debtor_name,debtor_iban,creditor_name,creditor_iban,amount'
ROW = 'A,DE89370400440532013000,B,NL91ABNA0417164300,12.50'

class CsvIntegrity(unittest.TestCase):
    def test_duplicate_normalized_headers(self):
        for extra in ('amount', ' Amount ', 'AMOUNT', 'amount!'):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, 'duplicate normalized'):
                rows_from_csv(HEAD + ',' + extra + '\n' + ROW + ',99')

    def test_alias_collisions(self):
        for extra in ('amt', 'value'):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, 'conflicting CSV aliases'):
                rows_from_csv(HEAD + ',' + extra + '\n' + ROW + ',99')
        with self.assertRaisesRegex(ValueError, 'conflicting CSV aliases'):
            rows_from_csv(HEAD + ',payer_name\n' + ROW + ',X')

    def test_aliases_resolve_and_quoted_fields(self):
        text = ('payer_name,from_iban,payee_name,to_iban,amt,ccy,reference\n'
                '"ACME, Inc.",DE89370400440532013000,"B ""Shop""",NL91ABNA0417164300,12.50,eur,"one, two\nthree"\n')
        rows = rows_from_csv(text)
        self.assertEqual(rows[0]['debtor_name'], 'ACME, Inc.')
        self.assertEqual(rows[0]['creditor_name'], 'B "Shop"')
        self.assertEqual(rows[0]['remittance'], 'one, two\nthree')
        root = ET.fromstring(build_pain001(rows, reviewed_input=True))
        self.assertEqual(root.find('.//p:InstdAmt', NS).text, '12.50')
        self.assertEqual(root.find('.//p:Ustrd', NS).text, 'one, two\nthree')

    def test_wrong_field_counts(self):
        for row in (ROW + ',50', ROW.rsplit(',', 1)[0], ',,,,,' , ',,,'):
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, 'fields'):
                rows_from_csv(HEAD + '\n' + row + '\n')
        # An unquoted decimal comma cannot silently move later values.
        with self.assertRaisesRegex(ValueError, 'fields'):
            rows_from_csv(HEAD + ',currency\n' + ROW[:-5] + '12,50,EUR\n')

    def test_bad_quotes(self):
        for row in ('"A,DE89370400440532013000,B,NL91ABNA0417164300,12.50',
                    '"A"oops,DE89370400440532013000,B,NL91ABNA0417164300,12.50'):
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, 'invalid CSV'):
                rows_from_csv(HEAD + '\n' + row)
        with self.assertRaisesRegex(ValueError, 'invalid CSV'):
            rows_from_csv('"bad header')

    def test_empty_header_rejected(self):
        with self.assertRaisesRegex(ValueError, 'empty CSV header'):
            rows_from_csv(HEAD + ',\n' + ROW + ',x')

class ArithmeticIntegrity(unittest.TestCase):
    def setUp(self):
        self.rows = rows_from_csv(SAMPLE_CSV)

    def test_low_decimal_context_is_irrelevant(self):
        self.rows[0]['amount'] = '123456789.12'
        self.rows[1]['amount'] = '0.89'
        with localcontext() as ctx:
            ctx.prec = 4
            root = ET.fromstring(build_pain001(self.rows, reviewed_input=True))
        self.assertEqual(root.find('.//p:GrpHdr/p:CtrlSum', NS).text, '123456790.01')
        self.assertEqual(root.find('.//p:PmtInf/p:CtrlSum', NS).text, '123456790.01')
        self.assertEqual([e.text for e in root.findall('.//p:InstdAmt', NS)], ['123456789.12', '0.89'])

    def test_limit_and_sum_overflow(self):
        self.rows[0]['amount'] = '9999999999999999.98'
        self.rows[1]['amount'] = '0.01'
        root = ET.fromstring(build_pain001(self.rows, reviewed_input=True))
        self.assertEqual(root.find('.//p:GrpHdr/p:CtrlSum', NS).text, '9999999999999999.99')
        self.rows[1]['amount'] = '0.02'
        with self.assertRaisesRegex(ValueError, 'control sum exceeds'):
            build_pain001(self.rows, reviewed_input=True)
        self.rows[0]['amount'] = '10000000000000000.00'
        with self.assertRaisesRegex(ValueError, 'amount exceeds'):
            build_pain001(self.rows, reviewed_input=True)
        self.rows[0]['amount'] = '9' * 5000 + '.00'
        with self.assertRaisesRegex(ValueError, 'amount exceeds'):
            build_pain001(self.rows, reviewed_input=True)

    def test_sum_across_groups_also_bounded(self):
        self.rows[0]['amount'] = '9999999999999999.99'
        self.rows[1]['amount'] = '0.01'
        self.rows[1]['execution_date'] = '2026-09-16'
        with self.assertRaisesRegex(ValueError, 'control sum exceeds'):
            build_pain001(self.rows, reviewed_input=True)

    def test_currency(self):
        for currency in ('USD', 'GBP', 'EU1', '€', 'EURO'):
            self.rows[0]['currency'] = currency
            with self.subTest(currency=currency), self.assertRaisesRegex(ValueError, 'currency must be EUR'):
                build_pain001(self.rows, reviewed_input=True)
        for currency in ('EUR', 'eur', '', '  EUR  '):
            self.rows[0]['currency'] = currency
            with self.subTest(currency=currency):
                self.assertEqual(ET.fromstring(build_pain001(self.rows, reviewed_input=True)).find('.//p:InstdAmt', NS).attrib['Ccy'], 'EUR')

if __name__ == '__main__':
    unittest.main()
