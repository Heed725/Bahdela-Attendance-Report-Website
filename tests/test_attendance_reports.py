import io
import unittest
from datetime import date, datetime, timedelta

from docx import Document
from openpyxl import load_workbook

from attendance_reports import subset_tables, export_subset


class AttendanceSubsetTests(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 11, 2)
        self.departments = {'Oil': {'label': 'OIL'}, 'Shop': {'label': 'SHOP'}}
        self.early = {'name': 'Early Employee', 'employee_id': '001', 'check_in': '06:50', 'check_out': '-', 'hours': '-'}
        self.on_time = {'name': 'On Time Employee', 'employee_id': '002', 'check_in': '07:00', 'check_out': '14:00', 'hours': '7 hrs 00 min'}
        self.late = {'name': 'Late Employee', 'employee_id': '003', 'check_in': '07:10', 'check_out': '-', 'hours': '-'}
        self.days = [(self.day, {'Oil': {'present': [self.early, self.on_time, self.late], 'absent': ['Absent Employee']}}),
                     (self.day + timedelta(days=1), {'Oil': {'present': [], 'absent': ['Early Employee', 'On Time Employee', 'Late Employee', 'Absent Employee']}})]

    def tables(self, mode):
        return subset_tables(self.days, self.departments, mode,
                             lambda dept, arrival: arrival.replace(hour=7, minute=0),
                             lambda dept, name: '004' if name == 'Absent Employee' else '001')

    def test_early_excludes_on_time_late_and_absent_and_retains_open_checkin(self):
        tables = self.tables('Early Attendance')
        self.assertEqual([r['Employee Name'] for r in tables[0][2]], ['Early Employee'])
        self.assertEqual(tables[0][2][0]['Minutes Early'], 10)
        self.assertEqual(tables[0][2][0]['Check Out'], '')
        self.assertEqual(tables[1][2], [])

    def test_checked_in_includes_pending_checkouts_and_absences_are_daily(self):
        present = self.tables('Checked In Only')
        self.assertEqual(len(present[0][2]), 3)
        self.assertEqual(present[1][2], [])
        absent = self.tables('Absentees')
        self.assertEqual([r['Employee Name'] for r in absent[0][2]], ['Absent Employee'])
        self.assertEqual(len(absent[1][2]), 4)
        self.assertTrue(all(r['Date'] == '2026-11-03' for r in absent[1][2]))

    def test_early_uses_allocated_shift_date_across_midnight(self):
        row = {**self.early, 'check_in': '23:55'}
        tables = subset_tables([(self.day, {'Oil': {'present': [row], 'absent': []}})],
                               self.departments, 'Early Attendance',
                               lambda dept, arrival: (arrival + timedelta(days=1)).replace(hour=0, minute=0),
                               lambda dept, name: '')
        self.assertEqual(tables[0][2][0]['Minutes Early'], 5)
        self.assertEqual(tables[0][2][0]['Shift Start'], '2026-11-03 00:00')

    def test_exports_match_filtered_records_and_have_no_lunch_instructions(self):
        for mode in ['Early Attendance', 'Checked In Only', 'Absentees']:
            with self.subTest(mode=mode):
                tables = self.tables(mode)[:1]
                files = export_subset(mode, 'Ocean Road', self.day, self.day, tables)
                self.assertTrue(files['pdf'].startswith(b'%PDF'))
                document = Document(io.BytesIO(files['docx']))
                text = ' '.join(p.text for p in document.paragraphs)
                self.assertIn(mode, text)
                self.assertNotIn('four clockings', text)
                self.assertNotIn('lunch', text.lower())
                expected = [r['Employee Name'] for r in tables[0][2]]
                names = [row.cells[2].text for row in document.tables[0].rows[1:]]
                self.assertEqual(names, expected)
                workbook = load_workbook(io.BytesIO(files['xlsx']))
                sheet = workbook.active
                self.assertEqual(sheet.title, mode)
                self.assertEqual([sheet.cell(i, 3).value for i in range(8, 8 + len(expected))], expected)
                if mode != 'Absentees':
                    self.assertEqual(sheet['D8'].value, '001')

    def test_zero_matches_still_exports_without_inventing_absentees(self):
        tables = self.tables('Early Attendance')[1:]
        files = export_subset('Early Attendance', 'Ocean', self.day, self.day, tables)
        self.assertTrue(files['pdf'].startswith(b'%PDF'))
        self.assertEqual(len(Document(io.BytesIO(files['docx'])).tables[0].rows), 1)
