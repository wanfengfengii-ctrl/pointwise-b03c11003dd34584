"""Exact rational parsing/formatting and ``y = a*x + b`` calibration helpers.

Everything on the wire may be written as an integer, a decimal string, a
``"n/d"`` string, a ``[numerator, denominator]`` pair, or an
``{"n": ..., "d": ...}`` object.  Internally we always use ``Fraction`` so
conversions are exact and reproducible.
"""

from __future__ import annotations

import math
import re
from fractions import Fraction

from .errors import HandoffError


def parse_int(value, field: str = "value") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HandoffError(
            "invalid_integer", f"{field} must be an integer", field=field, got=value
        )
    return value


def parse_rational(value, field: str = "value") -> Fraction:
    """Parse an exact rational.

    Floats are accepted only when they have a finite decimal representation;
    the decimal spelling (``repr``) is used so ``0.1`` means ``1/10``.
    """

    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a rational")
        if isinstance(value, int):
            return Fraction(value)
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("non-finite number")
            return Fraction(repr(value))
        if isinstance(value, str):
            text = value.strip().replace(" ", "")
            if not text:
                raise ValueError("empty rational")
            if text.startswith("+"):
                text = text[1:]
            return Fraction(text)
        if isinstance(value, (list, tuple)) and len(value) == 2:
            numerator = parse_int(value[0], f"{field}.numerator")
            denominator = parse_int(value[1], f"{field}.denominator")
            if denominator == 0:
                raise ZeroDivisionError("zero denominator")
            return Fraction(numerator, denominator)
        if isinstance(value, dict):
            for numerator_key, denominator_key in (
                ("n", "d"),
                ("num", "den"),
                ("numerator", "denominator"),
            ):
                if numerator_key in value and denominator_key in value:
                    numerator = parse_int(value[numerator_key], f"{field}.numerator")
                    denominator = parse_int(
                        value[denominator_key], f"{field}.denominator"
                    )
                    if denominator == 0:
                        raise ZeroDivisionError("zero denominator")
                    return Fraction(numerator, denominator)
            raise ValueError("missing numerator/denominator")
        raise ValueError("unsupported rational representation")
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise HandoffError(
            "invalid_rational",
            f"{field} is not a valid rational: {value!r}",
            field=field,
            got=value,
            reason=str(exc),
        ) from None


def format_rational(value: Fraction) -> str:
    """Canonical exact spelling: ``"2"``, ``"-1/3"``, ``"1/2"``."""

    value = Fraction(value)
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _parse_linear_side(text: str) -> Fraction:
    """Parse the coefficient left of ``x`` (``""`` -> 1, ``"-"`` -> -1)."""

    if text in ("", "+"):
        return Fraction(1)
    if text == "-":
        return Fraction(-1)
    if text.endswith("*"):
        text = text[:-1]
    return parse_rational(text, "calibration.coefficient")


def parse_calibration(value, field: str = "calibration") -> tuple[Fraction, Fraction]:
    """Parse ``y = a*x + b`` from a structured value or a short formula.

    Accepted spellings include ``{"a": 1, "b": "1/2"}``, ``[1, "1/2"]``,
    ``"1/2"`` (meaning ``b``? no - always a pair/formula) and ``"y=x+1/2"``.
    """

    if isinstance(value, dict):
        if "a" in value or "b" in value:
            a = parse_rational(value.get("a", 1), f"{field}.a")
            b = parse_rational(value.get("b", 0), f"{field}.b")
            return a, b
        raise HandoffError(
            "invalid_calibration",
            f"{field} object needs a/b coefficients",
            field=field,
            got=value,
        )

    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            raise HandoffError(
                "invalid_calibration",
                f"{field} pair must have exactly two entries",
                field=field,
                got=value,
            )
        return (
            parse_rational(value[0], f"{field}.a"),
            parse_rational(value[1], f"{field}.b"),
        )

    if isinstance(value, str):
        text = value.strip().lower().replace(" ", "")
        if text.startswith("y="):
            text = text[2:]
        if "x" not in text:
            raise HandoffError(
                "invalid_calibration",
                f"{field} must be a formula in x",
                field=field,
                got=value,
            )
        if text.count("x") != 1:
            raise HandoffError(
                "invalid_calibration",
                f"{field} must be linear in x",
                field=field,
                got=value,
            )
        left, right = text.split("x", 1)
        a = _parse_linear_side(left)
        b = Fraction(0) if right in ("", "+") else parse_rational(right, f"{field}.b")
        return a, b

    raise HandoffError(
        "invalid_calibration",
        f"unsupported calibration representation: {value!r}",
        field=field,
        got=value,
    )


def apply_calibration(a: Fraction, b: Fraction, raw: int) -> Fraction:
    return Fraction(a) * int(raw) + Fraction(b)


def calibration_formula(a: Fraction, b: Fraction) -> str:
    a = Fraction(a)
    b = Fraction(b)
    if a == 1:
        left = "x"
    elif a == -1:
        left = "-x"
    else:
        left = f"{format_rational(a)}*x"
    if b == 0:
        right = ""
    elif b > 0:
        right = f"+{format_rational(b)}"
    else:
        right = f"-{format_rational(abs(b))}"
    return f"y={left}{right}"


def looks_like_formula(text: str) -> bool:
    return bool(re.search(r"[xy=]", text))
