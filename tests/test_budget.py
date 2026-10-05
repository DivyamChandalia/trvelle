import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from trvelle.tools.itinerary_tool import PlannedItem
from trvelle.utils.budget import budget_breakdown
from trvelle.utils.currency import present_currency


class BudgetTests(unittest.TestCase):
    def test_approximate_ranges_budget_upper_bound_and_convert_both_bounds(self):
        cost = {'min_price': 10, 'max_price': 20, 'currency': 'USD', 'scope': 'per_person', 'status': 'estimate', 'basis': 'Typical admission from model knowledge; unverified.'}
        model = PlannedItem(title='Museum', cost=cost)
        self.assertEqual(model.cost.price, 20)
        trip = {'summary': {'currency': 'USD', 'travelers': 6}, 'daily_plan': [{'items': [{'card_type': 'activity', 'cost': cost}]}]}
        self.assertEqual(budget_breakdown(trip)['estimated_total'], 120)
        self.assertEqual(budget_breakdown(trip)['quoted_total'], 0)
        with patch('trvelle.utils.currency.exchange_rate', AsyncMock(return_value=(80, '2026-10-05'))):
            shown = asyncio.run(present_currency(trip, 'INR'))
        shown_cost = shown['daily_plan'][0]['items'][0]['cost']
        self.assertEqual((shown_cost['min_price'], shown_cost['max_price']), (800, 1600))
        self.assertEqual(shown['budget_breakdown']['estimated_total'], 9600)
        self.assertEqual(cost['max_price'], 20)

    def test_invalid_or_mislabeled_ranges_are_rejected(self):
        from pydantic import ValidationError
        for extra in ({'min_price': 10}, {'min_price': 20, 'max_price': 10}, {'min_price': 10, 'max_price': 20, 'status': 'quoted'}, {'min_price': 10, 'max_price': float('nan')}):
            with self.assertRaises(ValidationError):
                PlannedItem(title='Museum', cost={'currency': 'INR', 'scope': 'per_person', **extra})
    def trip(self):
        return {'summary': {'currency': 'INR', 'travelers': 2, 'budget_amount': 10000},
                'travel_options': {'flights': [{'selected': True, 'legs': [{'price': 2000, 'currency': 'INR'}, {'price': 4000, 'currency': 'INR'}]}],
                                   'hotels': [{'selected': True, 'stay_key': 'one-stay', 'currency': 'INR', 'total_rate': {'extracted_lowest': 2000}}]},
                'daily_plan': [{'items': []}]}

    def test_party_round_trip_and_whole_stay_count_once(self):
        trip = self.trip()
        trip['travel_options']['hotels'].append({**trip['travel_options']['hotels'][0], 'choose_uid': 'duplicate'})
        result = budget_breakdown(trip)
        self.assertEqual(result['quoted_total'], 6000)
        self.assertEqual(result['remaining_after_known'], 4000)
        self.assertEqual(result['planned_total'], 10000)
        self.assertEqual(result['unallocated'], 0)

    def test_scope_combined_tickets_estimates_and_unknown_are_distinct(self):
        trip = self.trip()
        ticket = {'price': 100, 'currency': 'INR', 'scope': 'per_person', 'status': 'quoted', 'coverage_key': 'museum-pass'}
        trip['daily_plan'][0]['items'] = [
            {'card_type': 'activity', 'cost': ticket}, {'card_type': 'activity', 'cost': ticket},
            {'card_type': 'activity', 'cost': {'price': 0, 'currency': 'INR', 'scope': 'party', 'status': 'quoted'}},
            {'card_type': 'activity'}, {'card_type': 'activity', 'cost': {'price': 50, 'currency': 'INR'}},
            {'card_type': 'meal', 'cost': {'price': 150, 'currency': 'INR', 'scope': 'party', 'status': 'estimate'}}]
        result = budget_breakdown(trip)
        rows = {row['key']: row for row in result['rows']}
        self.assertEqual(rows['activities']['quoted'], 200)
        self.assertEqual(rows['activities']['priced_count'], 2)  # A sourced free visit is priced.
        self.assertEqual(rows['activities']['unpriced_count'], 2)
        self.assertEqual(result['estimated_total'], 150)

    def test_foreign_or_invalid_costs_are_not_summed_as_local_quotes(self):
        trip = self.trip()
        trip['daily_plan'][0]['items'] = [{'card_type': 'activity', 'cost': {'price': value, 'currency': currency, 'scope': 'party'}}
                                        for value, currency in [(12, 'EUR'), (-5, 'INR'), (float('nan'), 'INR'), (True, 'INR')]]
        result = budget_breakdown(trip)
        self.assertEqual(result['known_total'], 6000)
        self.assertEqual(next(row for row in result['rows'] if row['key'] == 'activities')['unpriced_count'], 4)

    def test_saved_allowances_cannot_hide_known_expenses_or_over_budget(self):
        trip = self.trip()
        trip['daily_plan'][0]['items'] = [{'card_type': 'activity', 'cost': {'price': 1000, 'currency': 'INR', 'scope': 'party'}}]
        trip['budget_allocations'] = {'currency': 'INR', 'activities': 100, 'meals': 3000, 'transport': 500, 'buffer': 500}
        result = budget_breakdown(trip)
        self.assertEqual(result['planned_total'], 11000)
        self.assertEqual(result['unallocated'], -1000)
        self.assertEqual(result['allocation_source'], 'saved')

    def test_currency_converts_ticket_scope_and_saved_allowances_together(self):
        trip = {'summary': {'currency': 'USD', 'travelers': 2, 'budget_amount': 100},
                'budget_allocations': {'currency': 'USD', 'activities': 20, 'meals': 30, 'transport': 10, 'buffer': 5},
                'daily_plan': [{'items': [{'card_type': 'activity', 'cost': {'price': 10, 'currency': 'USD', 'scope': 'per_person', 'status': 'quoted'}}]}]}
        with patch('trvelle.utils.currency.exchange_rate', AsyncMock(return_value=(80, '2026-10-04'))):
            shown = asyncio.run(present_currency(trip, 'INR'))
        self.assertEqual(shown['daily_plan'][0]['items'][0]['cost']['price'], 800)
        self.assertEqual(shown['budget_allocations']['meals'], 2400)
        self.assertEqual(shown['budget_breakdown']['known_total'], 1600)
        self.assertEqual(shown['budget_breakdown']['budget'], 8000)
        self.assertEqual(trip['budget_allocations']['meals'], 30)

    def test_cost_schema_requires_currency_scope_and_nonnegative_finite_amount(self):
        from pydantic import ValidationError
        for cost in ({'price': 10}, {'price': -5, 'currency': 'INR', 'scope': 'party'}, {'price': float('inf'), 'currency': 'INR', 'scope': 'party'}):
            with self.assertRaises(ValidationError):
                PlannedItem(title='Museum', cost=cost)
        self.assertEqual(PlannedItem(title='Park', cost={'price': 0, 'currency': 'INR', 'scope': 'party', 'status': 'quoted'}).cost.price, 0)
