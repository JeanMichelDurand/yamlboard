"""The detail's table: the sort a header click gives, and the cells it shows."""

import pandas as pd

from yamlboard.sort_table import cells, next_order


def test_a_click_on_a_title_sorts_ascending_then_descending_then_unsorted():
    assert next_order([], "a", None) == [("a", False)]
    assert next_order([("a", False)], "a", None) == [("a", True)]
    assert next_order([("a", True)], "a", None) == []
    assert next_order([("a", True)], "b", None) == [("b", False)]   # another column starts ascending
    assert next_order([("b", False), ("a", True)], "a", None) == [("a", False)]  # alone again, from the start


def test_a_click_on_an_arrow_replaces_the_sort_and_unsorts_when_clicked_again():
    assert next_order([("b", False), ("c", True)], "a", True) == [("a", True)]
    assert next_order([("a", True)], "a", True) == []
    assert next_order([("a", True)], "a", False) == [("a", False)]
    assert next_order([("a", True), ("b", False)], "a", True) == [("a", True)]  # not the only key: kept alone


def test_a_shift_click_adds_reverses_or_removes_a_key_in_place():
    assert next_order([("a", False)], "b", True, add=True) == [("a", False), ("b", True)]
    assert next_order([("a", False), ("b", True)], "a", True, add=True) == [("a", True), ("b", True)]
    assert next_order([("a", False), ("b", True)], "b", True, add=True) == [("a", False)]
    assert next_order([("a", False), ("b", True)], "a", None, add=True) == [("a", True), ("b", True)]
    assert next_order([("a", True), ("b", True)], "a", None, add=True) == [("b", True)]
    assert next_order([], "a", None, add=True) == [("a", False)]


def test_cells_show_empty_values_as_none():
    df = pd.DataFrame({"n": [1.0, None, 2.5], "t": pd.to_datetime(["2026-03-01 00:00", None, "2026-03-02 10:00"]),
                       "s": ["x", None, "z"]})
    assert cells(df) == [["1", "2026-03-01 00:00:00", "x"], [None, None, None],
                         ["2.5", "2026-03-02 10:00:00", "z"]]


def test_numbers_read_with_two_decimals_and_spaced_thousands_by_default():
    from decimal import Decimal

    from yamlboard.sort_table import NumberFormat

    df = pd.DataFrame({"cost": [1e9, 1234.5678, None], "big": [Decimal("1069335487.000000000000000000789"), None, None],
                       "n": [1234567, 3, 0], "q": [12.0, None, 3.0], "s": ["1000", "x", None]})
    rows = cells(df, NumberFormat(), integers={"q"})
    nb = " "
    assert rows[0] == [f"1{nb}000{nb}000{nb}000.00", f"1{nb}069{nb}335{nb}487.00", f"1{nb}234{nb}567", "12", "1000"]
    assert rows[1] == [f"1{nb}234.57", None, "3", None, "x"]   # integers: no decimals, even read as floats
    assert cells(df, NumberFormat(0, "1 234,56"))[1][0] == f"1{nb}235"
    assert cells(df, NumberFormat(3, "1 234,56"))[1][0] == f"1{nb}234,568"
    assert cells(df, NumberFormat(1, "1,234.56"))[0][0] == "1,000,000,000.0"
    assert cells(df, NumberFormat(2, "1234.56"))[0][0] == "1000000000.00"


def test_is_number_is_a_python_bool():  # the component's data is JSON: a numpy bool turns it into text
    from yamlboard.sort_table import is_number

    assert is_number(pd.Series(["a", None])) is False and is_number(pd.Series([1, 2])) is True
