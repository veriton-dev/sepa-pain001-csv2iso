import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from sepa_pain001 import SAMPLE_CSV, build_pain001, rows_from_csv, self_test


class ApiGuard(unittest.TestCase):
    def test_explicit_boolean_required_per_call(self):
        rows = rows_from_csv(SAMPLE_CSV)
        with self.assertRaisesRegex(ValueError, 'reviewed_input=True'):
            build_pain001(rows)
        for value in (False, None, 1, 'yes', object()):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'reviewed_input=True'):
                build_pain001(rows, reviewed_input=value)
        ET.fromstring(build_pain001(rows, reviewed_input=True))
        with self.assertRaisesRegex(ValueError, 'reviewed_input=True'):
            build_pain001(rows)  # a prior acknowledgement does not persist

    def test_self_test_guard_before_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaisesRegex(ValueError, 'reviewed_input=True'):
            self_test()
        self.assertEqual(output.getvalue(), '')
        with contextlib.redirect_stdout(output):
            self.assertEqual(self_test(reviewed_input=True), 0)
        self.assertIn('<Document', output.getvalue())

    def test_cli_self_test_and_output_file(self):
        script = str(Path(__file__).with_name('sepa_pain001.py'))
        for extra in ([], ['--reviewed-input']):
            result = subprocess.run([sys.executable, script, '--self-test', *extra], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0 if extra else 2, result.stderr)
            self.assertEqual('<Document' in result.stdout, bool(extra))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'out.xml'
            path.write_text('untouched')
            result = subprocess.run([sys.executable, script, '-', '-o', str(path)], input=SAMPLE_CSV, text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(path.read_text(), 'untouched')
            self.assertNotIn('<Document', result.stdout)
            result = subprocess.run([sys.executable, script, '-', '-o', str(path), '--reviewed-input'], input=SAMPLE_CSV, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            ET.fromstring(path.read_text())

    def test_sample_csv_does_not_generate_xml(self):
        script = str(Path(__file__).with_name('sepa_pain001.py'))
        result = subprocess.run([sys.executable, script, '--sample-csv'], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, SAMPLE_CSV)
        self.assertNotIn('<Document', result.stdout)


if __name__ == '__main__':
    unittest.main()
