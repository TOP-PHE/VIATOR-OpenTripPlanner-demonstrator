"""A small PromQL reader for the monitoring tests (issue #374).

It parses the subset of PromQL VIATOR's dashboards and alerts use, with a
recursive descent over tokens, and checks what promtool cannot know: that each
metric and label is one the exporters really export. promtool itself runs in
CI (step "promtool" of .github/workflows/ci.yml) and remains the authority on
syntax; this reader catches the same classes of mistakes locally:

  * brackets, label matchers (with a comma between two), regexes that compile;
  * operands and binary operators alternating; `bool` only after a comparison;
  * range functions (rate, irate, increase, changes) given a range `[d]`,
    other functions and operators given instant vectors, no bare range left;
  * known functions and aggregations, their `by`/`without` clauses;
  * every label of a matcher exported by that metric; every grouping or
    matching label exported by at least one metric of the expression.

It is not a full PromQL parser: subqueries, `offset`, `@`, `topk`-style
parameters and string functions are refused, since nothing here uses them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

RANGE_FUNCTIONS = {"rate", "irate", "increase", "changes", "max_over_time"}
INSTANT_FUNCTIONS = {"absent"}
AGGREGATIONS = {"sum", "avg", "max", "min", "count"}
SET_OPERATORS = {"and", "or", "unless"}
ARITHMETIC = {"+", "-", "*", "/", "%", "^"}
COMPARISON = {"==", "!=", ">", "<", ">=", "<="}

_TOKEN = re.compile(
    r"""\s*(?:
      (?P<string>"(?:[^"\\]|\\.)*")
    | (?P<range>\[\s*\d+[smhdwy]\s*\])
    | (?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
    | (?P<ident>[A-Za-z_:][A-Za-z0-9_:]*)
    | (?P<op>==|!=|>=|<=|=~|!~|[-+*/%^<>=(){},])
    )""",
    re.VERBOSE,
)


def tokenize(expr: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while expr[pos:].strip():
        m = _TOKEN.match(expr, pos)
        assert m, f"cannot read {expr[pos:]!r} in {expr!r}"
        kind = m.lastgroup
        assert kind is not None
        tokens.append((kind, m.group(kind)))
        pos = m.end()
    return tokens


class Reader:
    """Parse one expression; `labels_of(metric)` gives a metric's labels and
    raises AssertionError for a metric no exporter has."""

    def __init__(self, expr: str, labels_of: Callable[[str], set[str]]) -> None:
        self.expr = expr
        self.tokens = tokenize(expr)
        self.pos = 0
        self.labels_of = labels_of
        self.metrics: set[str] = set()
        self.grouping: list[str] = []

    # ── token helpers ───────────────────────────────────────────────────────
    def peek(self) -> str | None:
        return self.tokens[self.pos][1] if self.pos < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        assert self.pos < len(self.tokens), f"unexpected end of {self.expr!r}"
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, value: str) -> None:
        got = self.take()[1]
        assert got == value, f"expected {value!r}, got {got!r} in {self.expr!r}"

    # ── grammar ─────────────────────────────────────────────────────────────
    def read(self) -> set[str]:
        kind = self.expression()
        assert self.peek() is None, f"unexpected {self.peek()!r} in {self.expr!r}"
        assert kind != "range", f"a range vector cannot be the result: {self.expr!r}"
        assert self.metrics, f"no metric in {self.expr!r}"
        known = set().union(*(self.labels_of(m) for m in self.metrics))
        for label in self.grouping:
            assert label in known, f"grouping label {label!r} is not exported in {self.expr!r}"
        return self.metrics

    def expression(self) -> str:
        kind = self.unary()
        while self.peek() in ARITHMETIC | COMPARISON | SET_OPERATORS:
            op = self.take()[1]
            assert kind != "range", f"operator {op!r} on a range vector in {self.expr!r}"
            if self.peek() == "bool":
                assert op in COMPARISON, f"bool after {op!r} in {self.expr!r}"
                self.take()
            self.vector_matching()
            right = self.unary()
            assert right != "range", f"operator {op!r} on a range vector in {self.expr!r}"
            kind = "instant"
        return kind

    def vector_matching(self) -> None:
        if self.peek() in ("on", "ignoring"):
            self.take()
            self.label_list()
            if self.peek() in ("group_left", "group_right"):
                self.take()
                if self.peek() == "(":
                    self.label_list()

    def label_list(self) -> None:
        self.expect("(")
        while self.peek() != ")":
            kind, value = self.take()
            assert kind == "ident", f"bad label {value!r} in {self.expr!r}"
            self.grouping.append(value)
            if self.peek() == ",":
                self.take()
        self.expect(")")

    def unary(self) -> str:
        if self.peek() in ("-", "+"):
            self.take()
        return self.primary()

    def primary(self) -> str:
        kind, value = self.take()
        if kind == "number":
            return "scalar"
        if value == "(":
            inner = self.expression()
            self.expect(")")
            return inner
        assert kind == "ident", f"unexpected {value!r} in {self.expr!r}"
        if value in AGGREGATIONS:
            return self.aggregation()
        if self.peek() == "(":
            return self.function(value)
        return self.selector(value)

    def aggregation(self) -> str:
        if self.peek() in ("by", "without"):
            self.take()
            self.label_list()
        self.expect("(")
        assert self.expression() != "range", f"aggregation of a range vector in {self.expr!r}"
        self.expect(")")
        if self.peek() in ("by", "without"):
            self.take()
            self.label_list()
        return "instant"

    def function(self, name: str) -> str:
        assert name in RANGE_FUNCTIONS | INSTANT_FUNCTIONS, f"unknown function {name!r}"
        self.expect("(")
        arg = self.expression()
        self.expect(")")
        wanted = "range" if name in RANGE_FUNCTIONS else "instant"
        assert arg == wanted, f"{name}() needs a {wanted} vector in {self.expr!r}"
        return "instant"

    def selector(self, metric: str) -> str:
        allowed = self.labels_of(metric)
        self.metrics.add(metric)
        if self.peek() == "{":
            self.take()
            self.matchers(metric, allowed)
        if self.pos < len(self.tokens) and self.tokens[self.pos][0] == "range":
            self.take()
            return "range"
        return "instant"

    def matchers(self, metric: str, allowed: set[str]) -> None:
        while self.peek() != "}":
            kind, label = self.take()
            assert kind == "ident", f"bad label {label!r} in {self.expr!r}"
            assert label in allowed, f"{metric} has no label {label!r}"
            op = self.take()[1]
            assert op in ("=", "!=", "=~", "!~"), f"bad matcher {op!r} in {self.expr!r}"
            kind, value = self.take()
            assert kind == "string", f"matcher value {value!r} is not a string"
            if op in ("=~", "!~"):
                re.compile(json.loads(value))
            if self.peek() != "}":
                self.expect(",")
        self.expect("}")


def read_expr(expr: str, labels_of: Callable[[str], set[str]]) -> set[str]:
    """The metric names `expr` reads; AssertionError (or re.error) if it is wrong."""
    return Reader(expr, labels_of).read()
