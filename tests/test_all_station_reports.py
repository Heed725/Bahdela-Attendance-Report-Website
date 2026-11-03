import unittest
from datetime import date
from unittest.mock import MagicMock

from all_station_reports import collect_station_reports, combine_station_tables
from hikvision import HikvisionClient, HikvisionError, ApiResult


class AllStationTests(unittest.TestCase):
    def test_failed_event_reads_are_skipped_and_empty_active_station_is_included(self):
        good, offline, incomplete = MagicMock(), MagicMock(), MagicMock()
        good.search_users.return_value = []
        good.search_events.return_value = []
        offline.search_users.side_effect = HikvisionError('Offline')
        incomplete.search_users.return_value = []
        incomplete.search_events.side_effect = HikvisionError('Incomplete events')
        config = {'label': 'Station', 'employees': {}, 'dept_order': [], 'dept_colors': {}, 'password': 'never retain'}
        clients = {'Good': good, 'Offline': offline, 'Incomplete': incomplete}
        active, skipped = collect_station_reports(clients, {name: config for name in clients}, date(2026, 11, 3), date(2026, 11, 3))
        self.assertEqual(list(active), ['Good'])
        self.assertEqual(set(skipped), {'Offline', 'Incomplete'})
        self.assertNotIn('password', active['Good']['roster'])
        offline.search_events.assert_not_called()

    def test_identical_employee_ids_at_different_stations_are_not_merged(self):
        table = [('03 Nov', ['ID', 'Employee Name'], [{'ID': '001', 'Employee Name': 'Same Name'}])]
        combined = combine_station_tables({'A': table, 'B': table})
        self.assertEqual(combined[0][1], ['Station', 'ID', 'Employee Name'])
        self.assertEqual([r['Station'] for r in combined[0][2]], ['A', 'B'])

    def test_event_pagination_and_incomplete_response(self):
        client = HikvisionClient('http://test', 'test', 'test')
        client._request = MagicMock(side_effect=[
            ApiResult(True, 200, {'AcsEvent': {'totalMatches': 2, 'InfoList': [{'employeeNoString': '001', 'time': 'a'}]}}),
            ApiResult(True, 200, {'AcsEvent': {'totalMatches': 2, 'InfoList': [{'employeeNoString': '002', 'time': 'b'}]}}),
        ])
        rows = client.search_events(date(2026, 11, 3), date(2026, 11, 3))
        self.assertEqual([r['employeeNoString'] for r in rows], ['001', '002'])
        self.assertEqual(client._request.call_args.kwargs['json']['AcsEventCond']['searchResultPosition'], 1)
        client._request = MagicMock(return_value=ApiResult(True, 200, {'AcsEvent': {'totalMatches': 1, 'InfoList': []}}))
        with self.assertRaisesRegex(HikvisionError, 'incomplete'):
            client.search_events(date(2026, 11, 3), date(2026, 11, 3))

    def test_repeated_event_page_is_rejected(self):
        client = HikvisionClient('http://test', 'test', 'test')
        client._request = MagicMock(return_value=ApiResult(True, 200, {'AcsEvent': {
            'totalMatches': 3, 'InfoList': [{'employeeNoString': '001', 'time': 'a'}]}}))
        with self.assertRaisesRegex(HikvisionError, 'repeated'):
            client.search_events(date(2026, 11, 3), date(2026, 11, 3))
