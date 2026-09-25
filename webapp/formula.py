"""Formulas and conditions for the reviewer's formula builder, compiled to ClickHouse SQL.

Nothing the user types reaches SQL as written. A formula is tokenized and parsed here,
and only known pieces are emitted: fields from the export's column list, number and
text literals (quoted and escaped), the operators below and the whitelisted functions.
A condition arrives as JSON from the query builder and is compiled the same way.

Formulas are Excel-like:

    [Amount] * 1.05
    ROUND([Quantity] * 2.5, 2)
    IF([Island] = 2, [Amount] * 0.9, [Amount])
    "MAHE " & UPPER([Tariff group])

Fields are written [Label] (as shown in the grid) or by their export name (AMOUNT).
Columns a reviewer added (ub_custom.py) are written [Their name] too; inside, they are
"x:<key>" and read from the row's `extra` map. A formula reads the values the row has
before the change.
"""
import re

from review import CODES, COLS, DATES, LABELS, NUMBER, WHOLE

MAX_FORMULA = 600
MAX_RULES = 60
MAX_DEPTH = 5

NUMERIC = NUMBER | WHOLE | set(CODES)          # read as numbers in a formula
BY_NAME = {c.lower(): c for c in COLS}
BY_NAME.update({LABELS[c].lower(): c for c in COLS if c in LABELS})


class FormulaError(ValueError):
    def __init__(self, msg: str, pos: int | None = None):
        super().__init__(msg)
        self.pos = pos


def sql_text(v: str) -> str:
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


CUSTOM: dict = {}  # "x:<key>" -> {"key", "name", "kind"}: the added columns, reloaded per request


def load_custom(extra: dict | None = None) -> None:
    """The added columns as they are now (plus, while previewing a new column, that one)."""
    import ub_custom
    CUSTOM.clear()
    for c in ub_custom.columns():
        CUSTOM[f"x:{c['key']}"] = c
    if extra:
        CUSTOM[f"x:{extra['key']}"] = extra


def is_custom(col: str) -> bool:
    return col.startswith("x:")


def field(name: str, pos: int | None = None) -> str:
    n = name.strip().lower()
    col = BY_NAME.get(n) or next((k for k, c in CUSTOM.items() if c["name"].lower() == n or k == n), None)
    if not col:
        raise FormulaError(f"Unknown field [{name}]", pos)
    return col


def ref(col: str) -> str:
    """The SQL for a field's text: a column, or an added column's value in `extra`."""
    return f"extra[{sql_text(CUSTOM[col]['key'])}]" if is_custom(col) else col


def is_num(col: str) -> bool:
    return CUSTOM[col]["kind"] == "number" if is_custom(col) else col in NUMERIC


def label(col: str) -> str:
    return CUSTOM[col]["name"] if is_custom(col) and col in CUSTOM else LABELS.get(col, col)


def read(col: str):
    """A field as a formula value: (sql, type)."""
    return (f"toFloat64OrZero({ref(col)})", "num") if is_num(col) else (ref(col), "txt")


# ------------------------------------------------------------------ tokens

TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|\.\d+)
  | (?P<str>"(?:[^"]|"")*"|'(?:[^']|'')*')
  | (?P<field>\[[^\]]*\])
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op><=|>=|<>|!=|==|[-+*/&(),=<>])
""", re.X)


def tokenize(src: str) -> list[tuple[str, str, int]]:
    if len(src) > MAX_FORMULA:
        raise FormulaError(f"A formula can be at most {MAX_FORMULA} characters")
    out, i = [], 0
    while i < len(src):
        m = TOKEN.match(src, i)
        if not m:
            raise FormulaError(f"Unexpected character {src[i]!r}", i)
        kind = m.lastgroup
        if kind != "ws":
            out.append((kind, m.group(), i))
        i = m.end()
    out.append(("end", "", len(src)))
    return out


# ------------------------------------------------------------------ parser: (sql, type) with type num | txt | bool

def as_num(e):
    sql, t = e
    if t == "num":
        return sql
    if t == "bool":
        return f"toFloat64({sql})"
    return f"toFloat64OrZero({sql})"


def as_txt(e):
    sql, t = e
    if t == "txt":
        return sql
    if t == "bool":
        return f"if({sql}, 'TRUE', 'FALSE')"
    return f"toString({sql})"


def as_bool(e, pos=None):
    sql, t = e
    if t == "bool":
        return sql
    raise FormulaError("Expected a condition here, like [Amount] > 100", pos)


def _round_digits(e, pos):
    sql, t = e
    if t != "num" or not re.fullmatch(r"toFloat64\(-?\d+\)", sql):
        raise FormulaError("The number of decimals must be a whole number, like 2", pos)
    n = int(sql[10:-1])
    if not 0 <= n <= 8:
        raise FormulaError("Decimals must be between 0 and 8", pos)
    return n


def xround(x: str, n: int) -> str:
    """Round like Excel: halves away from zero (2.5 -> 3, 2.675 -> 2.68). A float's shortest text
    ('2.675') read as a Decimal rounds exactly; Float64 round() would give 2 and 2.67."""
    return f"ifNull(toFloat64(round(toDecimal128OrNull(toString({x}), 12), {n})), round({x}, {n}))"


def xround_text(x: str, n: int) -> str:
    """ROUND like Excel, written as text straight from the exact decimal: through a Float64 on the
    way back, 9876543.21 comes out as '9876543.209999999'. Trailing zeros go ('26.20' -> '26.2')."""
    d = f"toString(round(toDecimal128OrNull(toString({x}), 12), {n}))"
    trimmed = f"replaceRegexpOne(replaceRegexpOne({d}, '0+$', ''), '\\\\.$', '')"
    return f"ifNull(if(position({d}, '.') > 0, {trimmed}, {d}), toString(round({x}, {n})))"


def _same(a, b):  # IF / MIN / MAX: both numbers, else both text
    return ("num", as_num(a), as_num(b)) if a[1] == b[1] == "num" else ("txt", as_txt(a), as_txt(b))


FUNCS = {  # name: (min args, max args, help)
    "ROUND": (1, 2, "ROUND(number, decimals)"), "ABS": (1, 1, "ABS(number)"),
    "MIN": (2, 8, "MIN(a, b, ...)"), "MAX": (2, 8, "MAX(a, b, ...)"),
    "IF": (3, 3, "IF(condition, then, else)"), "AND": (2, 8, "AND(cond, cond, ...)"),
    "OR": (2, 8, "OR(cond, cond, ...)"), "NOT": (1, 1, "NOT(condition)"),
    "CONCAT": (1, 8, "CONCAT(a, b, ...)"), "UPPER": (1, 1, "UPPER(text)"), "LOWER": (1, 1, "LOWER(text)"),
    "TRIM": (1, 1, "TRIM(text)"), "LEFT": (2, 2, "LEFT(text, n)"), "RIGHT": (2, 2, "RIGHT(text, n)"),
    "REPLACE": (3, 3, "REPLACE(text, find, with)"), "LEN": (1, 1, "LEN(text)"),
    "CONTAINS": (2, 2, "CONTAINS(text, part)"), "ISBLANK": (1, 1, "ISBLANK(value)"),
    "NUMBER": (1, 1, "NUMBER(text)"), "TEXT": (1, 1, "TEXT(value)"),
}


def call(name: str, args: list, pos: int):
    lo, hi, usage = FUNCS[name]
    if not lo <= len(args) <= hi:
        raise FormulaError(f"{name} takes {lo if lo == hi else f'{lo} to {hi}'} value(s): {usage}", pos)
    if name == "ROUND":
        return (xround(as_num(args[0]), _round_digits(args[1], pos) if len(args) > 1 else 0), "num")
    if name == "ABS":
        return (f"abs({as_num(args[0])})", "num")
    if name in ("MIN", "MAX"):
        fn = "least" if name == "MIN" else "greatest"
        if all(a[1] == "num" for a in args):
            return (f"{fn}({', '.join(as_num(a) for a in args)})", "num")
        return (f"{fn}({', '.join(as_txt(a) for a in args)})", "txt")
    if name == "IF":
        t, a, b = _same(args[1], args[2])
        return (f"if({as_bool(args[0], pos)}, {a}, {b})", t)
    if name in ("AND", "OR"):
        return (f"{name.lower()}({', '.join(as_bool(a, pos) for a in args)})", "bool")
    if name == "NOT":
        return (f"not({as_bool(args[0], pos)})", "bool")
    if name == "CONCAT":
        return (f"concat({', '.join(as_txt(a) for a in args)})" if len(args) > 1 else as_txt(args[0]), "txt")
    if name in ("UPPER", "LOWER"):
        return (f"{name.lower()}UTF8({as_txt(args[0])})", "txt")
    if name == "TRIM":
        return (f"trimBoth({as_txt(args[0])})", "txt")
    if name == "LEFT":
        return (f"leftUTF8({as_txt(args[0])}, toUInt32(greatest(0, {as_num(args[1])})))", "txt")
    if name == "RIGHT":
        return (f"rightUTF8({as_txt(args[0])}, toUInt32(greatest(0, {as_num(args[1])})))", "txt")
    if name == "REPLACE":
        return (f"replaceAll({as_txt(args[0])}, {as_txt(args[1])}, {as_txt(args[2])})", "txt")
    if name == "LEN":
        return (f"toFloat64(lengthUTF8({as_txt(args[0])}))", "num")
    if name == "CONTAINS":
        return (f"positionCaseInsensitiveUTF8({as_txt(args[0])}, {as_txt(args[1])}) > 0", "bool")
    if name == "ISBLANK":
        return (f"empty(trimBoth({as_txt(args[0])}))", "bool")
    if name == "NUMBER":
        return (as_num((as_txt(args[0]), "txt")), "num")
    if name == "TEXT":
        return (as_txt(args[0]), "txt")
    raise FormulaError(f"Unknown function {name}", pos)


class Parser:
    def __init__(self, src: str):
        self.toks = tokenize(src)
        self.i = 0
        self.fields = set()

    def peek(self, *vals):
        k, v, _ = self.toks[self.i]
        return v.upper() in vals if vals else (k, v)

    def take(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, v):
        k, got, pos = self.take()
        if got != v:
            raise FormulaError(f"Expected '{v}'" + (f" but found '{got}'" if got else " before the end"), pos)

    def parse(self):
        if self.toks[0][0] == "end":
            raise FormulaError("The formula is empty", 0)
        e = self.compare()
        k, v, pos = self.toks[self.i]
        if k != "end":
            raise FormulaError(f"Unexpected '{v}'", pos)
        return e

    def compare(self):
        a = self.concat()
        if self.peek("=", "==", "<>", "!=", "<", ">", "<=", ">="):
            _, op, pos = self.take()
            b = self.concat()
            op = {"==": "=", "<>": "!=", }.get(op, op)
            if a[1] == "num" or b[1] == "num":
                # a number on either side compares as numbers ([Region] = 2)
                return (f"({as_num(a)} {op} {as_num(b)})", "bool")
            return (f"({as_txt(a)} {op} {as_txt(b)})", "bool")
        return a

    def concat(self):
        a = self.add()
        while self.peek("&"):
            self.take()
            b = self.add()
            a = (f"concat({as_txt(a)}, {as_txt(b)})", "txt")
        return a

    def add(self):
        a = self.mul()
        while self.peek("+", "-"):
            _, op, _ = self.take()
            b = self.mul()
            a = (f"({as_num(a)} {op} {as_num(b)})", "num")
        return a

    def mul(self):
        a = self.unary()
        while self.peek("*", "/"):
            _, op, _ = self.take()
            b = self.unary()
            a = (f"({as_num(a)} {op} {as_num(b)})", "num")
        return a

    def unary(self):
        if self.peek("-"):
            self.take()
            return (f"(-{as_num(self.unary())})", "num")
        if self.peek("+"):
            self.take()
            return (as_num(self.unary()), "num")
        return self.atom()

    def atom(self):
        k, v, pos = self.take()
        if k == "num":
            return (f"toFloat64({v})", "num")
        if k == "str":
            return (sql_text(v[1:-1].replace(v[0] * 2, v[0])), "txt")
        if k == "field":
            col = field(v[1:-1], pos)
            self.fields.add(col)
            return read(col)
        if k == "name":
            up = v.upper()
            if up in ("TRUE", "FALSE"):
                return ("1" if up == "TRUE" else "0", "bool")
            if self.peek("("):
                if up not in FUNCS:
                    raise FormulaError(f"Unknown function {v}. Use one of: {', '.join(sorted(FUNCS))}", pos)
                self.take()
                args = []
                if not self.peek(")"):
                    args.append(self.compare())
                    while self.peek(","):
                        self.take()
                        args.append(self.compare())
                self.expect(")")
                return call(up, args, pos)
            col = field(v, pos)  # a bare export column name: AMOUNT
            if is_custom(col):
                raise FormulaError(f"Write added columns in brackets: [{label(col)}]", pos)
            self.fields.add(col)
            return read(col)
        if v == "(":
            e = self.compare()
            self.expect(")")
            return e
        if k == "end":
            raise FormulaError("The formula ends too early", pos)
        raise FormulaError(f"Unexpected '{v}'", pos)


def compile_formula(src: str) -> tuple[str, str, set]:
    """-> (sql, type, fields read). Raises FormulaError with the position of the problem."""
    p = Parser(str(src or ""))
    sql, t = p.parse()
    return sql, t, p.fields


def value_sql(target: str, src: str) -> str:
    """The text a target column gets from a formula, formatted the way the export writes it."""
    sql, t, _ = compile_formula(src)
    if t == "bool":
        raise FormulaError("This formula gives TRUE/FALSE; a field needs a value", 0)
    if is_custom(target) and CUSTOM[target]["kind"] == "number":  # blank stays blank
        if t == "txt":
            return f"if(trimBoth({sql}) = '', '', {xround_text(f'toFloat64OrZero(trimBoth({sql}))', 4)})"
        return xround_text(sql, 4)
    if is_custom(target):
        return as_txt((sql, t))
    if target in ("AMOUNT", "QUANTITY"):
        return xround_text(as_num((sql, t)), 4)
    if target in WHOLE or target in CODES or target == "CURDATETICKS":
        return f"toString(toInt64(round({as_num((sql, t))})))"
    return as_txt((sql, t))


def invalid_sql(target: str, src: str) -> str:
    """A condition true where the formula gives a value this column cannot hold."""
    sql, t, _ = compile_formula(src)
    # text going into a number field must read as a number, not quietly become 0
    not_num = f"isNull(toFloat64OrNull(trimBoth({sql}))) OR " if t == "txt" else ""
    if is_custom(target):
        kind = CUSTOM[target]["kind"]
        if kind == "number":
            if t == "txt":
                return (f"trimBoth({sql}) != '' AND (isNull(toFloat64OrNull(trimBoth({sql}))) OR "
                        f"abs(toFloat64OrZero(trimBoth({sql}))) >= 1e14)")
            return f"not (isFinite({as_num((sql, t))}) AND abs({as_num((sql, t))}) < 1e14)"
        if kind == "date":
            target = "INVOICEDATE"  # the same date rule
        else:
            return f"lengthUTF8({as_txt((sql, t))}) > 500"
    if target in ("AMOUNT", "QUANTITY"):  # stored as Decimal(18, 4): anything bigger would become 0
        return f"{not_num}not (isFinite({as_num((sql, t))}) AND abs({as_num((sql, t))}) < 1e14)"
    if target == "CURDATETICKS":
        return f"{not_num}not (isFinite({as_num((sql, t))}) AND abs({as_num((sql, t))}) < 9e18)"
    if target in WHOLE:
        return f"{not_num}not (isFinite({as_num((sql, t))}) AND round({as_num((sql, t))}) BETWEEN 0 AND 999)"
    if target in CODES:
        return f"{not_num}round({as_num((sql, t))}) NOT IN ({', '.join(str(c) for c in CODES[target])})"
    if target in DATES:
        v = as_txt((sql, t))
        return f"NOT ({v} = '' OR match({v}, '^\\\\d{{4}}-\\\\d{{2}}-\\\\d{{2}}( \\\\d{{2}}:\\\\d{{2}}(:\\\\d{{2}})?)?$'))"
    return f"lengthUTF8({as_txt((sql, t))}) > 500"


# ------------------------------------------------------------------ conditions (the query builder)

OPS = {"eq": "is", "ne": "is not", "gt": ">", "ge": "≥", "lt": "<", "le": "≤", "between": "between",
       "contains": "contains", "not_contains": "does not contain", "starts": "starts with", "ends": "ends with",
       "in": "is one of", "empty": "is empty", "not_empty": "is not empty"}
NUM_RE = re.compile(r"-?\d+(\.\d+)?")


def _cmp(col: str, op: str, value) -> str:
    num, lab, c = is_num(col), label(col), ref(col)
    if op in ("empty", "not_empty"):
        return f"{'' if op == 'empty' else 'NOT '}empty(trimBoth({c}))"
    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise FormulaError(f"{lab} between needs two values")
        a, b = (str(x).strip() for x in value)
        if num:
            if not (NUM_RE.fullmatch(a) and NUM_RE.fullmatch(b)):
                raise FormulaError(f"{lab} between needs two numbers")
            return f"toFloat64OrZero({c}) BETWEEN {a} AND {b}"
        return f"{c} BETWEEN {sql_text(a)} AND {sql_text(b)}"
    if op == "in":
        items = [x.strip() for x in (value if isinstance(value, list) else str(value).split(",")) if str(x).strip()]
        if not items or len(items) > 200:
            raise FormulaError(f"{lab} is one of: give 1 to 200 values, separated by commas")
        return f"trimBoth({c}) IN ({', '.join(sql_text(x) for x in items)})"
    v = str(value if value is not None else "").strip()
    if len(v) > 200:
        raise FormulaError("A value can be at most 200 characters")
    if op in ("contains", "not_contains"):
        return f"{'NOT ' if op == 'not_contains' else ''}positionCaseInsensitiveUTF8({c}, {sql_text(v)}) > 0"
    if op == "starts":
        return f"startsWith(lowerUTF8({c}), lowerUTF8({sql_text(v)}))"
    if op == "ends":
        return f"endsWith(lowerUTF8({c}), lowerUTF8({sql_text(v)}))"
    sym = {"eq": "=", "ne": "!=", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}.get(op)
    if not sym:
        raise FormulaError(f"Unknown comparison {op}")
    if num and NUM_RE.fullmatch(v):
        return f"toFloat64OrZero({c}) {sym} {v}"
    if num and op not in ("eq", "ne"):
        raise FormulaError(f"{lab} {OPS[op]} needs a number")
    return f"trimBoth({c}) {sym} {sql_text(v)}"


def compile_rule(rule: dict | None) -> str:
    """The query builder's rule tree -> a WHERE condition ('1' when empty)."""
    count = [0]

    def walk(node, depth):
        if depth > MAX_DEPTH:
            raise FormulaError("Groups are nested too deeply")
        if not isinstance(node, dict):
            raise FormulaError("Malformed condition")
        if "rules" in node:
            parts = [p for p in (walk(r, depth + 1) for r in node.get("rules") or []) if p]
            if not parts:
                return ""
            joiner = " OR " if node.get("combine") == "or" else " AND "
            inner = joiner.join(f"({p})" for p in parts)
            return f"NOT ({inner})" if node.get("negate") else inner
        count[0] += 1
        if count[0] > MAX_RULES:
            raise FormulaError(f"At most {MAX_RULES} conditions")
        if node.get("formula") is not None:  # a custom condition written as a formula
            sql, t, _ = compile_formula(node["formula"])
            if t != "bool":
                raise FormulaError("A formula condition must be TRUE or FALSE, like [Amount] > [Quantity] * 5")
            return sql
        return _cmp(field(str(node.get("field", ""))), str(node.get("op", "")), node.get("value"))

    return walk(rule or {"rules": []}, 0) or "1"


def describe_rule(rule: dict | None) -> str:
    """The rule in words, for the change log."""
    def walk(node):
        if "rules" in node:
            parts = [walk(r) for r in node.get("rules") or []]
            parts = [p for p in parts if p]
            s = (" or " if node.get("combine") == "or" else " and ").join(parts)
            return (f"not ({s})" if node.get("negate") else f"({s})" if len(parts) > 1 else s)
        if node.get("formula") is not None:
            return str(node["formula"])
        v = node.get("value")
        v = " and ".join(map(str, v)) if isinstance(v, list) else v
        lab = label(field(str(node.get("field", ""))))
        return f"{lab} {OPS.get(node.get('op'), node.get('op'))}" + ("" if node.get("op") in ("empty", "not_empty") else f" {v}")
    return walk(rule or {"rules": []}) or "all rows"
