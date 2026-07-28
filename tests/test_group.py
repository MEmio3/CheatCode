"""Focused tests for the August 1 Hall 6 group planner."""
from __future__ import annotations

import unittest

from cinebot.group import (
    GroupPlanError,
    choose_hall_show,
    display_show_time,
    movie_matches,
    plan_full_rows,
    validate_bkash_number,
    validate_names,
)
from cinebot.live.group_booking import payments_from_payload
from cinebot.seats.scorer import Seat, SeatMap, seatmap_from_view


class GroupPlannerTests(unittest.TestCase):
    def test_captured_hall6_rows_plan_as_10_7_10_7(self):
        seats = [
            Seat(row=index, col=column, row_label=row, col_label=str(column + 1),
                 available=True, seat_id=f"{row}{column + 1}")
            for index, row in enumerate(("E", "F"))
            for column in range(17)
        ]
        chunks = plan_full_rows(SeatMap(n_rows=2, n_cols=17, seats=seats))

        self.assertEqual([len(chunk.seats) for chunk in chunks], [10, 7, 10, 7])
        self.assertEqual(sum(len(chunk.seats) for chunk in chunks), 34)
        self.assertEqual(chunks[0].labels, tuple(f"E{i}" for i in range(1, 11)))
        self.assertEqual(chunks[1].labels, tuple(f"E{i}" for i in range(11, 18)))
        self.assertEqual(chunks[2].labels, tuple(f"F{i}" for i in range(1, 11)))
        self.assertEqual(chunks[3].labels, tuple(f"F{i}" for i in range(11, 18)))

    def test_full_row_plan_rejects_one_taken_seat(self):
        seats = [
            Seat(
                row=row_index,
                col=column,
                row_label=row,
                col_label=str(column + 1),
                available=not (row == "F" and column == 4),
                seat_id=f"{row}{column + 1}",
            )
            for row_index, row in enumerate(("E", "F"))
            for column in range(17)
        ]
        with self.assertRaisesRegex(GroupPlanError, "F5"):
            plan_full_rows(SeatMap(n_rows=2, n_cols=17, seats=seats))

    def test_movie_matching_tolerates_punctuation(self):
        self.assertTrue(movie_matches("SPIDER MAN – BRAND NEW DAY"))
        self.assertTrue(movie_matches("Spider-Man: Brand New Day (3D)"))

    def test_hall_show_is_filtered_and_closest_to_five(self):
        shows = [
            {
                "movieId": 42,
                "movieTitle": "Spider-Man: Brand New Day",
                "screenID": 6,
                "showTimes": [
                    {
                        "programId": 100,
                        "showTime": "16:10:00",
                        "seatPrices": [
                            {
                                "seatTypeID": 2,
                                "seatTypeTitle": "Premium",
                                "unitPrice": 600,
                            }
                        ],
                    },
                    {
                        "programId": 101,
                        "showTime": "17:10:00",
                        "seatPrices": [
                            {
                                "seatTypeID": 2,
                                "seatTypeTitle": "Premium",
                                "unitPrice": 700,
                            }
                        ],
                    },
                ],
            }
        ]
        choice = choose_hall_show(shows)
        self.assertEqual(choice.program_id, 101)
        self.assertEqual(choice.unit_price, 700)
        self.assertEqual(display_show_time(choice.show_time), "5:10 PM")

    def test_names_and_bkash_are_strict(self):
        self.assertEqual(
            validate_names(["A One", "B Two", "C Three", "D Four"], 4),
            ["A One", "B Two", "C Three", "D Four"],
        )
        with self.assertRaises(GroupPlanError):
            validate_names(["Same", "Same", "C", "D"], 4)
        self.assertEqual(validate_bkash_number("+8801712345678"), "01712345678")
        with self.assertRaises(GroupPlanError):
            validate_bkash_number("012345")

    def test_duplicate_identity_requires_explicit_override(self):
        payload = [
            {"name": "Meher", "bkash_number": "01712345678", "seats": ["E1"]},
            {"name": "Meher", "bkash_number": "01712345678", "seats": ["E2"]},
        ]
        with self.assertRaises(GroupPlanError):
            payments_from_payload(payload)
        self.assertEqual(
            len(payments_from_payload(payload, allow_duplicate_identity=True)),
            2,
        )


if __name__ == "__main__":
    unittest.main()

