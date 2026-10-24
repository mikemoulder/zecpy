"""Payroll CSV parsing, without a chain."""

import pytest

from examples.payroll import read_payroll, zec_to_zat


@pytest.mark.parametrize(("text", "zat"), [("1", 100_000_000), ("0.00000001", 1), ("1.25", 125_000_000), (" 0.8 ", 80_000_000)])
def test_exact_amounts(text, zat):
    assert zec_to_zat(text) == zat


@pytest.mark.parametrize("text", ["0", "-1", "0.000000001", "abc", ""])
def test_bad_amounts(text):
    with pytest.raises(ValueError):
        zec_to_zat(text)


def test_every_bad_row_is_reported(tmp_path):
    f = tmp_path / "p.csv"
    f.write_text("name,address,amount_zec,memo\nAda,u1a,1.5,\nBo,u1b,-2,\nCy,u1c,x,\n")
    with pytest.raises(ValueError) as e:
        read_payroll(f)
    assert "row 3" in str(e.value) and "row 4" in str(e.value) and "row 2" not in str(e.value)
