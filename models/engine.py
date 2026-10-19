"""Formula engine for the Vietnamese financial statements.

Every report line (line code, "mã số") carries a formula such as::

    PDR(1311) + PDR(3311)
    [110] + [120] - [130]
    NBAL(4212) + PL_CUR()

Line references are written ``[code]``. Functions take account code prefixes
(``131`` matches 131, 1311, 13881 does not). The engine never calls ``eval``:
formulas are parsed with :mod:`ast` and only whitelisted nodes are walked.

Balances come from a *provider*. :class:`LedgerProvider` reads posted journal
items; the consolidation module plugs in its own provider that adds the
members' ledgers and the elimination rows together.
"""

import ast
import re
from collections import defaultdict, namedtuple
from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo.exceptions import UserError
from odoo.tools.translate import LazyTranslate

_lt = LazyTranslate(__name__)

# One aggregated balance: account code, commercial partner id (or False),
# debit, credit. ``elim`` marks consolidation adjustments, which reduce
# turnover on the account's natural side instead of adding to the other one.
Row = namedtuple("Row", "code partner_id debit credit elim", defaults=(False,))

# Cash and cash equivalents (B01 code 110): moves between them are not cash flows.
CASH_PREFIXES = ("111", "112", "113", "12811", "12881")
# Pseudo codes the consolidation adds to profit and loss.
PL_PSEUDO_CODES = frozenset({"HN_EQPL", "HN_PLNCI"})

CHECK_CODE = "__check__"

KIND_POSITION = "position"
KIND_PERIOD = "period"
KIND_CASHFLOW = "cashflow"

LINE_REF_RE = re.compile(r"\[\s*([A-Za-z0-9_.]+)\s*\]")

MAIN_FUNCTIONS = {"BAL", "NBAL", "DR", "CR", "PDR", "PCR", "DT", "CT"}
STOCK_FUNCTIONS = {"OPEN", "CLOSE", "CHG"}
CASH_FUNCTIONS = {"CASH", "CASHC", "CASHD"}
REST_FUNCTIONS = {"CASH_REST_IN", "CASH_REST_OUT"}
PL_FUNCTIONS = {"PL_CUR", "PL_PRIOR"}
ALL_FUNCTIONS = (
    MAIN_FUNCTIONS | STOCK_FUNCTIONS | CASH_FUNCTIONS | REST_FUNCTIONS | PL_FUNCTIONS
)
FUNCTIONS_BY_KIND = {
    KIND_POSITION: MAIN_FUNCTIONS | PL_FUNCTIONS,
    KIND_PERIOD: MAIN_FUNCTIONS,
    KIND_CASHFLOW: MAIN_FUNCTIONS | STOCK_FUNCTIONS | CASH_FUNCTIONS | REST_FUNCTIONS,
}


def with_companies(env, companies):
    """``env`` allowed to read ``companies``, whichever companies are active in the UI.

    Record rules only show the companies selected in the company switcher; a
    report must read the companies it covers. The user still needs access to them.
    """
    if not env.su:
        denied = companies - env.user.sudo().company_ids
        if denied:
            raise UserError(
                env._("You don't have access to these companies: %s", ", ".join(denied.sudo().mapped("name")))
            )
    allowed = list(dict.fromkeys(companies.ids + list(env.context.get("allowed_company_ids") or [])))
    allowed = [cid for cid in allowed if env.su or cid in env.user.sudo().company_ids.ids]
    return env(context=dict(env.context, allowed_company_ids=allowed))


def is_pl_code(code):
    """Profit and loss accounts are classes 5 to 9 (911 included)."""
    return code[:1] in "56789" or code in PL_PSEUDO_CODES


def is_natural_credit(code):
    return code[:1] in "57" and not code.startswith("521")


def _matches(code, prefixes):
    return code.startswith(prefixes)


# ---------------------------------------------------------------------------
# Formula parsing
# ---------------------------------------------------------------------------


class FormulaError(ValueError):
    """A formula problem. ``message(env)`` gives it in the user's language."""

    def __init__(self, lazy_message):
        # The English text for logs and tracebacks: no environment is needed.
        super().__init__(lazy_message._source % lazy_message._args)
        self.lazy_message = lazy_message

    def message(self, env):
        return env._(self.lazy_message)


def parse_formula(formula):
    """Parse a line formula into an AST, or return None for an empty one."""
    if not formula or not formula.strip():
        return None
    source = LINE_REF_RE.sub(lambda m: f'_L("{m.group(1)}")', formula.strip())
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as e:
        raise FormulaError(_lt("Invalid formula: %(formula)s", formula=formula)) from e
    _validate(tree.body, formula)
    return tree.body


def _validate(node, formula):
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
    ):
        _validate(node.left, formula)
        _validate(node.right, formula)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        _validate(node.operand, formula)
    elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        pass
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        name = node.func.id
        if name != "_L" and name not in ALL_FUNCTIONS:
            raise FormulaError(_lt("Unsupported function: %(name)s (in: %(formula)s)", name=name, formula=formula))
        if node.keywords:
            raise FormulaError(_lt("Keyword arguments are not allowed (in: %(formula)s)", formula=formula))
        for arg in node.args:
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, (int, str))):
                raise FormulaError(
                    _lt("The arguments of %(name)s must be account codes (in: %(formula)s)", name=name, formula=formula)
                )
    else:
        raise FormulaError(_lt("Unsupported expression in formula: %(formula)s", formula=formula))


def iter_calls(node):
    """Yield (function name, prefixes) for every call in a parsed formula."""
    if node is None:
        return
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            yield sub.func.id, tuple(str(arg.value) for arg in sub.args)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class LedgerProvider:
    """Balances from the journal items of one legal entity (company + branches)."""

    def __init__(self, env, companies, posted_only=True):
        self.env = with_companies(env, companies)
        self.companies = companies.with_env(self.env)
        self.states = ("posted",) if posted_only else ("draft", "posted")
        self._cache = {}

    @property
    def currency(self):
        return self.companies[:1].currency_id

    def fiscal_year_start(self, date):
        return self.companies[:1].compute_fiscalyear_dates(date)["date_from"]

    def _domain(self, company, date_from, date_to):
        domain = [
            ("company_id", "=", company.id),
            ("parent_state", "in", self.states),
            ("account_id", "!=", False),
        ]
        if date_from:
            domain.append(("date", ">=", date_from))
        if date_to:
            domain.append(("date", "<=", date_to))
        return domain

    def _account_ids(self, company, domain, prefixes):
        """Accounts used in ``domain`` whose code starts with one of ``prefixes``."""
        AML = self.env["account.move.line"]
        accounts = self.env["account.account"].union(
            *(account for (account,) in AML._read_group(domain, ["account_id"]))
        )
        return [
            account.id
            for account in accounts.with_company(company)
            if _matches(account.code or "", prefixes)
        ]

    def _closing_move_ids(self, company, domain):
        """Moves that close profit and loss into account 911 (year-end closing)."""
        account_ids = self._account_ids(company, domain, ("911",))
        if not account_ids:
            return []
        lines = self.env["account.move.line"].search(
            domain + [("account_id", "in", account_ids)]
        )
        return lines.move_id.ids

    def _group(self, company, domain):
        groups = self.env["account.move.line"]._read_group(
            domain, ["account_id", "partner_id"], ["debit:sum", "credit:sum"]
        )
        return [
            Row(
                account.with_company(company).code,
                partner.commercial_partner_id.id or False,
                debit,
                credit,
            )
            for account, partner, debit, credit in groups
        ]

    def rows(self, date_from=None, date_to=None, exclude_closing=False):
        key = ("rows", date_from, date_to, exclude_closing)
        if key not in self._cache:
            result = []
            for company in self.companies:
                domain = self._domain(company, date_from, date_to)
                if exclude_closing:
                    closing = self._closing_move_ids(company, domain)
                    if closing:
                        domain = domain + [("move_id", "not in", closing)]
                result += self._group(company, domain)
            self._cache[key] = result
        return self._cache[key]

    def cash_rows(self, date_from, date_to):
        """Counterpart lines of the moves that touch cash.

        In a balanced move, the counterparts' (credit - debit) add up to the
        cash movement, so each counterpart line *is* its share of the cash flow.
        """
        key = ("cash", date_from, date_to)
        if key not in self._cache:
            AML = self.env["account.move.line"]
            result = []
            for company in self.companies:
                domain = self._domain(company, date_from, date_to)
                cash_ids = self._account_ids(company, domain, CASH_PREFIXES)
                if not cash_ids:
                    continue
                move_ids = AML.search(domain + [("account_id", "in", cash_ids)]).move_id.ids
                result += self._group(
                    company,
                    domain
                    + [("move_id", "in", move_ids), ("account_id", "not in", cash_ids)],
                )
            self._cache[key] = result
        return self._cache[key]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class _RowSet:
    """Rows plus the bookkeeping needed to warn about unmapped accounts."""

    def __init__(self, rows):
        self.rows = rows
        self.used = set()  # indexes of rows that contributed to some line

    def select(self, prefixes):
        return [(i, r) for i, r in enumerate(self.rows) if _matches(r.code, prefixes)]


class ReportEvaluator:
    """Compute every line of one report for one period."""

    def __init__(self, lines, provider, kind, date_from, date_to):
        """``lines``: iterable of (code, formula) pairs, in report order."""
        self.provider = provider
        self.kind = kind
        self.date_from = date_from
        self.date_to = date_to
        self.formulas = {}
        allowed = FUNCTIONS_BY_KIND[kind]
        cash_prefixes = set()
        for code, formula in lines:
            node = parse_formula(formula)
            for name, prefixes in iter_calls(node):
                if name in ALL_FUNCTIONS and name not in allowed:
                    raise FormulaError(
                        _lt("Function %(name)s cannot be used in this kind of report (line %(code)s)", name=name, code=code)
                    )
                if name in CASH_FUNCTIONS:
                    cash_prefixes.update(prefixes)
            self.formulas[code] = node
        self.cash_prefixes = tuple(cash_prefixes)
        self.values = {}
        self._stack = []
        self._sets = {}

    # -- row sets ----------------------------------------------------------

    def _set(self, name):
        if name not in self._sets:
            p, f, t = self.provider, self.date_from, self.date_to
            if name == "main":
                if self.kind == KIND_POSITION:
                    rows = p.rows(None, t)
                else:
                    rows = p.rows(f, t, exclude_closing=True)
            elif name == "pl_cur":
                rows = [r for r in p.rows(p.fiscal_year_start(t), t) if is_pl_code(r.code)]
            elif name == "pl_prior":
                start = p.fiscal_year_start(t)
                rows = [
                    r
                    for r in p.rows(None, start - timedelta(days=1))
                    if is_pl_code(r.code)
                ]
            elif name == "open":
                rows = p.rows(None, f - timedelta(days=1))
            elif name == "close":
                rows = p.rows(None, t)
            elif name == "cash":
                rows = p.cash_rows(f, t)
            else:
                raise KeyError(name)
            self._sets[name] = _RowSet(rows)
        return self._sets[name]

    # -- functions ---------------------------------------------------------

    def _balance(self, set_name, prefixes):
        rowset = self._set(set_name)
        total = 0.0
        for i, row in rowset.select(prefixes):
            rowset.used.add(i)
            total += row.debit - row.credit
        return total

    def _split(self, prefixes, by_partner, positive):
        """Debit (positive=True) or credit balances, per account or per partner."""
        rowset = self._set("main")
        groups = defaultdict(list)
        for i, row in rowset.select(prefixes):
            key = (row.code, row.partner_id) if by_partner else row.code
            groups[key].append((i, row))
        total = 0.0
        for members in groups.values():
            balance = sum(r.debit - r.credit for _i, r in members)
            if (balance > 0) == positive and balance:
                total += abs(balance)
                rowset.used.update(i for i, _r in members)
        return total

    def _turnover(self, prefixes, debit_side):
        rowset = self._set("main")
        total = 0.0
        for i, row in rowset.select(prefixes):
            rowset.used.add(i)
            if row.elim:
                # An elimination reduces the account's natural side.
                if is_natural_credit(row.code) != debit_side:
                    net = row.debit - row.credit
                    total += net if debit_side else -net
            else:
                total += row.debit if debit_side else row.credit
        return total

    def _cash(self, prefixes, side):
        rowset = self._set("cash")
        total = 0.0
        for i, row in rowset.select(prefixes):
            rowset.used.add(i)
            if side in ("net", "in"):
                total += row.credit
            if side in ("net", "out"):
                total -= row.debit
        return total

    def _cash_rest(self, side):
        rowset = self._set("cash")
        total = 0.0
        for row in rowset.rows:
            if self.cash_prefixes and _matches(row.code, self.cash_prefixes):
                continue
            net = row.credit - row.debit
            if (side == "in" and net > 0) or (side == "out" and net < 0):
                total += net
        return total

    def _pl(self, set_name):
        rowset = self._set(set_name)
        rowset.used.update(range(len(rowset.rows)))
        return sum(r.credit - r.debit for r in rowset.rows)

    def _call(self, name, prefixes):
        if name == "BAL":
            return self._balance("main", prefixes)
        if name == "NBAL":
            return -self._balance("main", prefixes)
        if name in ("DR", "CR", "PDR", "PCR"):
            return self._split(prefixes, name.startswith("P"), name.endswith("DR"))
        if name == "DT":
            return self._turnover(prefixes, debit_side=True)
        if name == "CT":
            return self._turnover(prefixes, debit_side=False)
        if name == "OPEN":
            return self._balance("open", prefixes)
        if name == "CLOSE":
            return self._balance("close", prefixes)
        if name == "CHG":
            return self._balance("close", prefixes) - self._balance("open", prefixes)
        if name == "CASH":
            return self._cash(prefixes, "net")
        if name == "CASHC":
            return self._cash(prefixes, "in")
        if name == "CASHD":
            return self._cash(prefixes, "out")
        if name == "CASH_REST_IN":
            return self._cash_rest("in")
        if name == "CASH_REST_OUT":
            return self._cash_rest("out")
        if name == "PL_CUR":
            return self._pl("pl_cur")
        if name == "PL_PRIOR":
            return self._pl("pl_prior")
        raise FormulaError(name)

    # -- walking -----------------------------------------------------------

    def _eval(self, node):
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.UnaryOp):
            value = self._eval(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            left, right = self._eval(node.left), self._eval(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            return left / right if right else 0.0
        args = tuple(str(arg.value) for arg in node.args)
        if node.func.id == "_L":
            return self.value(args[0]) or 0.0
        return self._call(node.func.id, args)

    def value(self, code):
        """Value of line ``code``; None when the line has no formula."""
        if code in self.values:
            return self.values[code]
        if code not in self.formulas:
            raise FormulaError(_lt("Unknown line code in the report: [%(code)s]", code=code))
        if code in self._stack:
            cycle = " → ".join(self._stack + [code])
            raise FormulaError(_lt("Circular reference between formulas: %(cycle)s", cycle=cycle))
        node = self.formulas[code]
        self._stack.append(code)
        try:
            result = None if node is None else self._eval(node)
        finally:
            self._stack.pop()
        self.values[code] = result
        return result

    def compute(self):
        for code in self.formulas:
            self.value(code)
        return self.values

    def unmapped(self):
        """{code: balance} of accounts with a balance that no line picked up."""
        if self.kind == KIND_CASHFLOW or "main" not in self._sets:
            return {}
        rowset = self._sets["main"]
        totals = defaultdict(float)
        for i, row in enumerate(rowset.rows):
            if i in rowset.used:
                continue
            if self.kind == KIND_POSITION and is_pl_code(row.code):
                continue  # picked up through PL_CUR / PL_PRIOR
            if self.kind == KIND_PERIOD and not is_pl_code(row.code):
                continue
            if row.code == "HN_PLNCI" and self.kind == KIND_PERIOD:
                continue
            totals[row.code] += row.debit - row.credit
        rounding = self.provider.currency.rounding or 0.01
        return {code: bal for code, bal in totals.items() if abs(bal) >= rounding}


COMPARE_DEFAULT = "default"  # the statutory form: B01 vs opening of the year, B02/B03 vs last year
COMPARE_PREVIOUS = "previous_period"
COMPARE_LAST_YEAR = "previous_year"
COMPARE_NONE = "none"


def _whole_months(date_from, date_to):
    """Number of months when the period runs from a 1st to a month end, else None."""
    if date_from.day != 1 or (date_to + timedelta(days=1)).day != 1:
        return None
    return (date_to.year - date_from.year) * 12 + date_to.month - date_from.month + 1


def _shift_back(date_from, date_to, steps):
    """The period ``steps`` periods of the same length before [date_from, date_to]."""
    months = _whole_months(date_from, date_to)
    if months:
        start = date_from - relativedelta(months=months * steps)
        return start, start + relativedelta(months=months, days=-1)
    days = (date_to - date_from).days + 1
    return date_from - timedelta(days=days * steps), date_to - timedelta(days=days * steps)


def comparison_periods(kind, date_from, date_to, mode=COMPARE_DEFAULT, count=1, fiscal_year_start=None):
    """[(date_from, date_to)] of the comparative columns, most recent first.

    For B01 only ``date_to`` matters (the position at that day); ``date_from`` of
    the current period still tells how long a "previous period" is.
    """
    if mode == COMPARE_NONE:
        return []
    periods = []
    for step in range(1, max(count, 1) + 1):
        if mode == COMPARE_PREVIOUS:
            periods.append(_shift_back(date_from, date_to, step))
        elif mode == COMPARE_LAST_YEAR or kind != KIND_POSITION:
            periods.append((date_from - relativedelta(years=step), date_to - relativedelta(years=step)))
        else:
            # Opening of the year ("Số đầu năm"): closing position of the previous fiscal year(s).
            year_start = fiscal_year_start(date_to) if fiscal_year_start else date_from
            opening = year_start - relativedelta(years=step - 1) - timedelta(days=1)
            periods.append((opening - relativedelta(years=1, days=-1), opening))
    return periods


def comparative_dates(kind, date_from, date_to):
    """Dates of the default (statutory form) comparative column."""
    return comparison_periods(kind, date_from, date_to)[0]


def evaluate_report(report, provider, date_from, date_to, with_comparative=True, comparisons=None):
    """Compute a ``vn.report`` record.

    ``comparisons`` lists the (date_from, date_to) of the comparative columns;
    by default, the one of the statutory form. Returns ``{"lines": [...], "warnings": [...]}``.
    Each line dict holds the line record plus ``value``, ``value_cmps`` (one per
    comparative column) and ``value_cmp`` (the first one); None when blank.
    """
    if date_from > date_to:
        raise UserError(provider.env._("The start date must be before the end date."))
    if comparisons is None:
        comparisons = (
            comparison_periods(
                report.kind, date_from, date_to, fiscal_year_start=provider.fiscal_year_start
            )
            if with_comparative
            else []
        )
    spec = [(line.code, line.formula) for line in report.line_ids]
    check_spec = [(CHECK_CODE, report.check_expression)] if report.check_expression else []
    try:
        current = ReportEvaluator(
            spec + check_spec, provider, report.kind, date_from, date_to
        )
        current.compute()
        comparatives = []
        for cmp_from, cmp_to in comparisons:
            evaluator = ReportEvaluator(spec, provider, report.kind, cmp_from, cmp_to)
            evaluator.compute()
            comparatives.append(evaluator)
    except FormulaError as e:
        raise UserError(e.message(provider.env)) from e

    env = provider.env
    currency = provider.currency
    warnings = []
    for code, balance in sorted(current.unmapped().items()):
        warnings.append(
            env._(
                "Account %(code)s has a balance of %(balance)s but is not used by any line of %(report)s.",
                code=code,
                balance=f"{currency.round(balance):,.0f}",
                report=report.code,
            )
        )
    if hasattr(provider, "extra_warnings"):
        warnings += provider.extra_warnings(date_from, date_to)
    if check_spec:
        difference = currency.round(current.values.get(CHECK_CODE) or 0.0)
        if difference:
            warnings.append(
                env._(
                    "%(message)s: difference of %(difference)s.",
                    message=report.check_message or env._("Check failed"),
                    difference=f"{difference:,.0f}",
                )
            )

    def rounded(value):
        return None if value is None else currency.round(value)

    lines = []
    for line in report.line_ids:
        value_cmps = [rounded(evaluator.values.get(line.code)) for evaluator in comparatives]
        lines.append(
            {
                "line": line,
                "value": rounded(current.values.get(line.code)),
                "value_cmps": value_cmps,
                "value_cmp": value_cmps[0] if value_cmps else None,
            }
        )
    return {"lines": lines, "warnings": warnings, "comparisons": comparisons}
