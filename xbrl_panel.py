#OTN Note: NVDA's Q1 2027 10-Q was issued on April 26, 2026: https://www.sec.gov/ix?doc=/Archives/edgar/data/0001045810/000104581026000052/nvda-20260426.htm#fact-identifier-340.
#I documented XBRL tags for each financial state within the NVDA-BS.csv, NVDA-IS.csv.

#!/usr/bin/env python3
"""
xbrl_panel.py — Build a historical financial-statement panel from SEC XBRL
company facts, driven by an FSLI -> XBRL tag mapping ("bank panel") CSV.

Inputs
------
  bank_panel_-_<TICKER>-BS.csv    FSLI / XBRL Tag  (balance sheet, instant facts)
  bank_panel_-_<TICKER>-IS.csv    FSLI / XBRL Tag  (income statement, duration facts)
  bank_panel_-_<TICKER>-SCF.csv   FSLI / XBRL Tag  (cash flows, YTD-derived, optional)

Output
------
  panel_balance_sheet.csv        FSLI rows x period columns
  panel_income_statement.csv     FSLI rows x period columns
  panel_cash_flow.csv            FSLI rows x period columns (only if --scf given)
  check_results.csv              every integrity check, per period, pass/fail
  facts_long.csv                 tidy long-format fact table (for Sheets/BI)
  tag_provenance.csv             which concept actually sourced each cell

Integrity checks
----------------
  BS01  Assets == LiabilitiesAndStockholdersEquity
  BS02  Assets == Liabilities + StockholdersEquity
  BS03  AssetsCurrent == sum(current asset components)
  BS04  LiabilitiesCurrent == sum(current liability components)
  BS05  StockholdersEquity == sum(equity components)
  IS01  Revenues - CostOfRevenue == GrossProfit
  IS02  GrossProfit - OperatingExpenses == OperatingIncomeLoss
  IS03  OperatingIncomeLoss + NonoperatingIncomeExpense == PretaxIncome
  IS04  PretaxIncome - IncomeTaxExpenseBenefit == NetIncomeLoss
  IS05  NetIncomeLoss + OCI == ComprehensiveIncomeNetOfTax
  EQ01  Equity rollforward:
        SE(t-1) + NetIncome + OCI + StockIssuedNewIssues + ShareBasedComp
              - TaxWithholdingOnShareBasedComp - ShareRepurchases - Dividends
        == SE(t)
  CF01  OperatingCF + InvestingCF + FinancingCF + FXEffect == Change in cash
  CF02  BeginningCash + Change in cash == EndingCash          (cash rollforward)
  CF03  EndingCash (SCF) == Cash on balance sheet             (cross-statement)
  CF04  NetIncomeLoss (SCF) == NetIncomeLoss (income stmt)    (cross-statement)

Notes on the cash flow statement
--------------------------------
  * 10-Q cash flow statements are filed YEAR-TO-DATE only (no native 3-month
    column), so discrete quarters are derived by differencing consecutive YTD
    figures within a fiscal year: Q1 = YTD(Q1); Qn = YTD(Qn) - YTD(Q(n-1));
    Q4 = FY - YTD(Q3). This mirrors the discrete-quarter convention already used
    for the income statement, so the three statements tie to one another.
  * "Cash at beginning/end of period" are INSTANT balances, not flows. They are
    read from the balance concept at the prior period-end and the current
    period-end respectively (not differenced).

Usage
-----
  python xbrl_panel.py --cik 1045810 --ticker NVDA \
      --bs "NVIDIA Financial Statement XBRL Tags - NVIDIA-BS.csv" --is "NVIDIA Financial Statement XBRL Tags - NVIDIA-IS.csv" --scf "NVIDIA Financial Statement XBRL Tags - NVIDIA-SCF (corrected).csv" \
      --user-agent "Ozoemena Nnamadim ozoemena@otnnamadim.com" --periods 12 --outdir ./out

  # no network — exercise the check engine against a local fixture
  python xbrl_panel.py --fixture fixture_companyfacts.json --bs ... --is ... --scf ...
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

import pandas as pd

try:
    import requests
except ImportError:  # offline/fixture mode does not need it
    requests = None


SEC_COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# ---------------------------------------------------------------------------
# Tolerances. SEC values are reported in whole units (USD), so ABS_TOL is in
# dollars. A check passes if it is within EITHER tolerance.
# ---------------------------------------------------------------------------
ABS_TOL = 1_000_000.0     # $1MM — absorbs rounding in $-in-millions filers
REL_TOL = 0.0025          # 25bps of the larger side


# ===========================================================================
# 1. Mapping ingestion
# ===========================================================================

TAG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*:[A-Za-z][A-Za-z0-9_\-\.]*$")


@dataclass
class MappedLine:
    order: int
    fsli: str            # cleaned label
    raw_fsli: str        # original, indentation preserved
    tag: str             # "us-gaap:Assets"
    concept: str         # "Assets"
    taxonomy: str        # "us-gaap"
    depth: int           # indentation level, from leading spaces
    statement: str       # "BS" | "IS" | "SCF"


def load_mapping(path: str, statement: str) -> list[MappedLine]:
    """Read a bank-panel CSV. Header sits on row 3 (two blank spacer rows)."""
    raw = pd.read_csv(path, header=None, dtype=str, keep_default_na=False)

    # locate the header row rather than hardcoding index 2
    hdr = None
    for i in range(min(10, len(raw))):
        row = [str(c).strip().lower() for c in raw.iloc[i].tolist()]
        if "fsli" in row and any("xbrl" in c for c in row):
            hdr = i
            break
    if hdr is None:
        raise ValueError(f"{path}: could not locate 'FSLI' / 'XBRL Tag' header row")

    body = raw.iloc[hdr + 1 :, :2]
    body.columns = ["fsli", "tag"]

    lines: list[MappedLine] = []
    for i, (fsli, tag) in enumerate(body.itertuples(index=False, name=None)):
        fsli = "" if fsli is None else str(fsli)
        tag = "" if tag is None else str(tag).strip()
        if not TAG_RE.match(tag):
            # section headers, blank spacers, and subtotal placeholders skipped
            continue
        depth = (len(fsli) - len(fsli.lstrip(" "))) // 5
        clean = fsli.strip().lstrip("-").strip()
        taxonomy, concept = tag.split(":", 1)
        lines.append(
            MappedLine(
                order=i,
                fsli=clean,
                raw_fsli=fsli.rstrip(),
                tag=tag,
                concept=concept,
                taxonomy=taxonomy,
                depth=depth,
                statement=statement,
            )
        )
    if not lines:
        raise ValueError(f"{path}: no valid XBRL tags found")
    if len(lines) < 5:
        print(f"WARNING: {path} yielded only {len(lines)} tagged lines; "
              f"check the file layout.", file=sys.stderr)
    return lines


# ===========================================================================
# 2. Tag fallback chains
# ===========================================================================
# A mapping sheet pins one tag per FSLI, but issuers migrate concepts across
# years. Without fallbacks, a panel silently shows blanks for older/newer
# periods. Each primary concept maps to ordered alternates.

FALLBACKS: dict[str, list[str]] = {
    "Revenues": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    ],
    "CostOfRevenue": [
        "CostOfGoodsAndServicesSold",
        "CostOfRevenueExcludingDepreciationDepletionAndAmortization",
    ],
    "DebtSecuritiesCurrent": [
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "MarketableSecuritiesCurrent",
    ],
    "EquitySecuritiesFvNi": ["EquitySecuritiesFvNiCurrentAndNoncurrent"],
    "PrepaidExpenseAndOtherAssetsCurrent": ["OtherAssetsCurrent"],
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "DebtCurrent": ["LongTermDebtCurrent", "CommercialPaper"],
    "PreferredStockValueOutstanding": ["PreferredStockValue"],
    "IntangibleAssetsNetExcludingGoodwill": ["FiniteLivedIntangibleAssetsNet"],
    "DeferredIncomeTaxAssetsNet": ["DeferredIncomeTaxAssetsNetNoncurrent"],
    "WeightedAverageNumberOfDilutedSharesOutstanding": [
        "WeightedAverageNumberOfDilutedSharesOutstandingBasicAndDiluted",
    ],

    # --- Statement of cash flows -------------------------------------------
    # Section subtotals: pre-2018 filings used the "...ContinuingOperations"
    # variants before the discontinued-operations split was normalized.
    "NetCashProvidedByUsedInOperatingActivities": [
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "NetCashProvidedByUsedInInvestingActivities": [
        "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
    ],
    "NetCashProvidedByUsedInFinancingActivities": [
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
    ],
    # ASU 2016-18 renamed the cash balance/change concepts to include
    # restricted cash. Older periods report the pre-restatement names.
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": [
        "CashAndCashEquivalentsAtCarryingValue",
    ],
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect": [
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",
        "CashAndCashEquivalentsPeriodIncreaseDecrease",
    ],
    "PaymentsOfDividends": ["PaymentsOfDividendsCommonStock"],
    "DepreciationDepletionAndAmortization": [
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
    ],
    "GainLossOnInvestments": ["GainLossOnInvestmentsAndDerivativeInstruments"],
}

# Concepts needed for the equity rollforward that do not appear on either
# mapping sheet (they live on the cash flow / equity statements).
ROLLFORWARD_CONCEPTS: dict[str, list[str]] = {
    "NetIncomeLoss": ["ProfitLoss"],
    "OtherComprehensiveIncomeLossNetOfTaxPortionAttributableToParent": [
        "OtherComprehensiveIncomeLossNetOfTax"
    ],
    "StockIssuedDuringPeriodValueNewIssues": [
        "ProceedsFromIssuanceOfCommonStock",
        "StockIssuedDuringPeriodValueEmployeeStockPurchasePlan",
        "ProceedsFromStockOptionsExercised",
    ],
    "ShareBasedCompensation": [
        "AllocatedShareBasedCompensationExpense",
        "ShareBasedCompensationArrangementByShareBasedPaymentAwardCompensationCost",
    ],
    "PaymentsRelatedToTaxWithholdingForShareBasedCompensation": [
        "AdjustmentsRelatedToTaxWithholdingForShareBasedCompensation",
        "StockRepurchasedDuringPeriodValue",
    ],
    "PaymentsForRepurchaseOfCommonStock": [
        "TreasuryStockValueAcquiredCostMethod",
        "StockRepurchasedAndRetiredDuringPeriodValue",
    ],
    "PaymentsOfDividendsCommonStock": [
        "PaymentsOfDividends",
        "DividendsCommonStockCash",
    ],
    "StockholdersEquity": ["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
}


def chain(concept: str) -> list[str]:
    return [concept] + FALLBACKS.get(concept, []) + ROLLFORWARD_CONCEPTS.get(concept, [])


# ===========================================================================
# 3. SEC fetch
# ===========================================================================

def fetch_companyfacts(cik: int, user_agent: str, cache: str | None = None) -> dict:
    """SEC requires a descriptive User-Agent and throttles at ~10 req/sec."""
    if cache and os.path.exists(cache):
        with open(cache) as fh:
            return json.load(fh)
    if requests is None:
        raise RuntimeError("requests not installed; use --fixture for offline mode")

    url = SEC_COMPANYFACTS.format(cik=cik)
    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    }
    for attempt in range(4):
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if cache:
                with open(cache, "w") as fh:
                    json.dump(data, fh)
            return data
        if resp.status_code in (403, 429, 503):
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
    raise RuntimeError(
        f"SEC returned {resp.status_code}. A real contact string in --user-agent "
        f"is mandatory; 403 almost always means the header was rejected."
    )


# ===========================================================================
# 4. Fact extraction
# ===========================================================================

@dataclass
class Fact:
    concept: str
    taxonomy: str
    value: float
    unit: str
    end: date
    start: date | None
    fy: int | None
    fp: str | None
    form: str
    filed: date
    frame: str | None = None

    @property
    def days(self) -> int | None:
        if self.start is None:
            return None
        return (self.end - self.start).days

    @property
    def is_instant(self) -> bool:
        return self.start is None


def _d(s: str | None) -> date | None:
    return datetime.strptime(s, "%Y-%m-%d").date() if s else None


def index_facts(companyfacts: dict) -> dict[str, list[Fact]]:
    """Flatten companyfacts JSON into {concept: [Fact, ...]}.

    Keyed by concept name only, so company-extension concepts (e.g. the
    ``nvda:`` tags on the cash flow statement) resolve by their local name
    regardless of taxonomy prefix.
    """
    out: dict[str, list[Fact]] = defaultdict(list)
    for taxonomy, concepts in companyfacts.get("facts", {}).items():
        for concept, payload in concepts.items():
            for unit, entries in payload.get("units", {}).items():
                for e in entries:
                    try:
                        out[concept].append(
                            Fact(
                                concept=concept,
                                taxonomy=taxonomy,
                                value=float(e["val"]),
                                unit=unit,
                                end=_d(e["end"]),
                                start=_d(e.get("start")),
                                fy=e.get("fy"),
                                fp=e.get("fp"),
                                form=e.get("form", ""),
                                filed=_d(e.get("filed")) or date.min,
                                frame=e.get("frame"),
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
    return out


FORM_RANK = {"10-K": 0, "10-K/A": 0, "10-Q": 1, "10-Q/A": 1, "8-K": 3, "20-F": 0}


def _pick(cands: list[Fact]) -> Fact | None:
    """Prefer periodic reports over 8-K, then the most recently filed value
    (restatements supersede originals)."""
    if not cands:
        return None
    return sorted(cands, key=lambda f: (FORM_RANK.get(f.form, 2), -f.filed.toordinal()))[0]


def instant_value(facts: dict[str, list[Fact]], concept: str, end: date) -> tuple[float | None, str | None]:
    for c in chain(concept):
        hits = [f for f in facts.get(c, []) if f.is_instant and f.end == end and f.unit == "USD"]
        f = _pick(hits)
        if f:
            return f.value, c
    return None, None


def duration_value(
    facts: dict[str, list[Fact]],
    concept: str,
    end: date,
    target_days: tuple[int, int],
    unit: str = "USD",
) -> tuple[float | None, str | None]:
    for c in chain(concept):
        hits = [
            f
            for f in facts.get(c, [])
            if not f.is_instant
            and f.end == end
            and f.unit in (unit, "USD/shares", "shares")
            and target_days[0] <= (f.days or 0) <= target_days[1]
        ]
        f = _pick(hits)
        if f:
            return f.value, c
    return None, None


QTR_DAYS = (80, 100)
YTD_DAYS = (350, 380)

# Concepts that are NOT additive across quarters. Deriving fiscal Q4 as
# FY - (Q1+Q2+Q3) is only valid for flows (revenue, expenses, income). Per-share
# amounts, weighted-average share counts, and point-in-time ratios/rates do not
# sum — subtracting them yields nonsense (e.g. a negative Q4 share count). For
# these, Q4 is left blank unless a native ~90-day fact exists, because a genuine
# discrete-quarter figure is simply not reported and cannot be reconstructed by
# subtraction. (Q4 EPS, if needed, is derived-Q4 net income over a period-end
# share count downstream — never by differencing EPS.)
NON_ADDITIVE_RE = re.compile(
    r"PerShare"
    r"|WeightedAverageNumberOf\w*Shares"
    r"|SharesOutstanding"
    r"|Ratio|Percentage|EffectiveIncomeTaxRate",
    re.IGNORECASE,
)


def _is_non_additive(concept: str) -> bool:
    return bool(NON_ADDITIVE_RE.search(concept))


def quarterly_value(
    facts: dict[str, list[Fact]], concept: str, period: "Period"
) -> tuple[float | None, str | None]:
    """Discrete-quarter value for INCOME-STATEMENT concepts.

    A natively reported ~90-day fact is used when present. Q4 is never filed on
    a 10-Q, so for additive flows it is derived as FY less the three prior
    quarters. Non-additive concepts (per-share amounts, weighted-average share
    counts, ratios) are never derived by subtraction — Q4 is left blank instead
    of a meaningless artefact.
    """
    v, used = duration_value(facts, concept, period.end, QTR_DAYS)
    if v is not None:
        return v, used

    if period.fp == "Q4" and period.prior_q_ends:
        if _is_non_additive(concept):
            return None, None   # not additive: no valid Q4 by subtraction
        fy_val, used = duration_value(facts, concept, period.end, YTD_DAYS)
        if fy_val is None:
            return None, None
        total = 0.0
        for qe in period.prior_q_ends:
            qv, _ = duration_value(facts, concept, qe, QTR_DAYS)
            if qv is None:
                return None, None
            total += qv
        return fy_val - total, (used + " [derived Q4]") if used else None
    return None, None


# ---------------------------------------------------------------------------
# Cash-flow specific extraction
# ---------------------------------------------------------------------------
# Cash flow statements are filed year-to-date only. A discrete quarter is the
# current cumulative figure minus the same fiscal year's prior-quarter
# cumulative figure. The two "beginning/ending cash" lines are instant balances
# and are handled separately (see build_panel / scf_line_role).

CF_CUM_DAYS = (60, 380)  # Q1 YTD (~90d) through full fiscal year (~365d)

# Canonical instant balance concept for beginning/ending cash on the SCF.
BALANCE_CASH = "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"


def cumulative_cashflow(
    facts: dict[str, list[Fact]], concept: str, end: date
) -> tuple[float | None, str | None]:
    """As-filed cumulative (YTD or full-year) flow ending on ``end``.

    Within one fiscal year there is exactly one cumulative duration per end
    date; when several spans share the end date (e.g. a stray 3-month IS fact
    also tagged under this concept) the widest span is the year-to-date figure.
    """
    for c in chain(concept):
        hits = [
            f
            for f in facts.get(c, [])
            if not f.is_instant
            and f.end == end
            and f.unit == "USD"
            and CF_CUM_DAYS[0] <= (f.days or 0) <= CF_CUM_DAYS[1]
        ]
        if hits:
            best = max(
                hits,
                key=lambda f: (f.days, -FORM_RANK.get(f.form, 2), f.filed.toordinal()),
            )
            return best.value, c
    return None, None


def cashflow_value(
    facts: dict[str, list[Fact]], concept: str, period: "Period"
) -> tuple[float | None, str | None]:
    """Discrete-quarter cash-flow value via year-to-date differencing.

        Q1: cumulative(Q1)                        (~1 quarter, nothing to net off)
        Qn: cumulative(Qn) - cumulative(Q(n-1))   (Q2, Q3)
        Q4: cumulative(FY) - cumulative(Q3)
    """
    cur, used = cumulative_cashflow(facts, concept, period.end)
    if cur is None:
        return None, None
    if period.fytd_prior_end is None:
        return cur, used  # Q1: cumulative == discrete
    prior, _ = cumulative_cashflow(facts, concept, period.fytd_prior_end)
    if prior is None:
        return None, None  # cannot difference safely -> report as missing
    return cur - prior, (used + " [derived discrete]") if used else None


def scf_line_role(ln: "MappedLine") -> str:
    """Classify an SCF mapping line into how its value must be sourced:

        begin_cash  -> instant balance at the prior period-end
        end_cash    -> instant balance at the current period-end
        change_cash -> duration (net change in cash), YTD-differenced
        flow        -> duration line item, YTD-differenced

    The balance lines are detected from the FSLI label because the shipped
    template mis-tags "beginning of period" with the change-in-cash concept.
    """
    f = ln.fsli.lower()
    if "begin" in f:
        return "begin_cash"
    if "end of period" in f or f.endswith("at end") or " at end" in f:
        return "end_cash"
    c = ln.concept
    if c.endswith("PeriodIncreaseDecreaseIncludingExchangeRateEffect") or \
       c.endswith("PeriodIncreaseDecreaseExcludingExchangeRateEffect") or \
       c == "CashAndCashEquivalentsPeriodIncreaseDecrease":
        return "change_cash"
    return "flow"


# ===========================================================================
# 5. Period scaffold
# ===========================================================================

@dataclass
class Period:
    end: date
    fy: int
    fp: str                       # Q1 | Q2 | Q3 | Q4
    prior_end: date | None = None          # immediately preceding period-end
    prior_q_ends: list[date] = field(default_factory=list)  # same-FY quarters (Q4 use)
    fytd_prior_end: date | None = None     # same-FY prior quarter-end (YTD differencing)

    @property
    def label(self) -> str:
        return f"FY{self.fy}{self.fp}"


def _infer_fye_month(facts: dict[str, list[Fact]]) -> int:
    """Infer the fiscal-year-end month from annual filings.

    The month an issuer closes its fiscal year on is stable, so we take the
    most common end month among annual (10-K / 20-F, or fp=='FY') Assets facts.
    NVIDIA closes in late January -> 1. Falls back to the modal end month of all
    Assets facts, then to December."""
    from collections import Counter
    annual_months = Counter()
    all_months = Counter()
    for f in facts.get("Assets", []):
        if not (f.is_instant and f.unit == "USD"):
            continue
        all_months[f.end.month] += 1
        if f.form.startswith(("10-K", "20-F")) or f.fp == "FY":
            annual_months[f.end.month] += 1
    if annual_months:
        return annual_months.most_common(1)[0][0]
    if all_months:
        return all_months.most_common(1)[0][0]
    return 12


def _fiscal_label(end: date, fye_month: int) -> tuple[int, str]:
    """Fiscal (year, quarter) for a period end date, from the fiscal calendar
    alone — never from a filing's reported fy/fp, which can carry the context of
    a later filing that references this date only as a prior-period comparative.

    A period ending in or before the fiscal-year-end month belongs to that
    fiscal year (its Q4); later months roll into the next fiscal year. The
    quarter is the whole-quarter offset from the fiscal-year start."""
    if end.month <= fye_month:
        fy = end.year
    else:
        fy = end.year + 1
    offset = (end.month - (fye_month + 1)) % 12   # months into the fiscal year
    q = offset // 3 + 1
    return fy, f"Q{q}"


def build_periods(facts: dict[str, list[Fact]], n: int) -> list[Period]:
    """Derive the reporting calendar from Assets instant facts (present in
    every filing) rather than assuming calendar quarter-ends.

    Fiscal-year and quarter labels come from each period's END DATE via the
    inferred fiscal-year-end month — NOT from the fact's own fy/fp fields. Those
    fields reflect the *filing* that reported the value, so the most recent
    annual balance (e.g. the fiscal Q4 that a later 10-Q carries as its
    prior-year comparative) would otherwise inherit that 10-Q's fy/fp and be
    mislabelled as the next Q1, colliding with the real Q1 of the new year."""
    assets = [f for f in facts.get("Assets", []) if f.is_instant and f.unit == "USD"]
    if not assets:
        raise RuntimeError("no us-gaap:Assets facts — cannot establish period grid")

    fye_month = _infer_fye_month(facts)

    by_end: dict[date, Fact] = {}
    for f in sorted(assets, key=lambda x: x.filed):
        by_end[f.end] = f  # last write wins = most recently filed

    ends = sorted(by_end)
    periods: list[Period] = []
    for i, e in enumerate(ends):
        fy, fp = _fiscal_label(e, fye_month)
        p = Period(end=e, fy=fy, fp=fp, prior_end=ends[i - 1] if i else None)
        periods.append(p)

    # three prior quarter-ends inside the same fiscal year (Q4 IS derivation)
    for i, p in enumerate(periods):
        if p.fp == "Q4":
            p.prior_q_ends = [q.end for q in periods[max(0, i - 3) : i]]

    # immediately-preceding quarter-end WITHIN the same fiscal year, for
    # year-to-date cash-flow differencing (None for the first quarter of a FY)
    for i, p in enumerate(periods):
        same_fy_prior = [q.end for q in periods[:i] if q.fy == p.fy and q.end < p.end]
        p.fytd_prior_end = max(same_fy_prior) if same_fy_prior else None

    return periods[-n:] if n else periods


# ===========================================================================
# 6. Panel assembly
# ===========================================================================

def build_panel(
    facts: dict[str, list[Fact]], lines: list[MappedLine], periods: list[Period]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, prov = [], []
    for ln in lines:
        row = {"FSLI": ln.raw_fsli or ln.fsli, "XBRL Tag": ln.tag}
        prow = dict(row)
        role = scf_line_role(ln) if ln.statement == "SCF" else None
        for p in periods:
            if ln.statement == "BS":
                v, used = instant_value(facts, ln.concept, p.end)
            elif ln.statement == "IS":
                v, used = quarterly_value(facts, ln.concept, p)
            else:  # SCF
                if role == "begin_cash":
                    v, used = (instant_value(facts, BALANCE_CASH, p.prior_end)
                               if p.prior_end else (None, None))
                elif role == "end_cash":
                    v, used = instant_value(facts, BALANCE_CASH, p.end)
                else:  # flow or change_cash
                    v, used = cashflow_value(facts, ln.concept, p)
            row[p.label] = v
            prow[p.label] = used or ""
        rows.append(row)
        prov.append(prow)
    return pd.DataFrame(rows), pd.DataFrame(prov)


def value_grid(facts: dict[str, list[Fact]], periods: list[Period], concepts: Iterable[str], kind: str) -> dict:
    """{(concept, period_label): value} for the concepts the checks need.

    kind: "instant" | "duration" (IS-style discrete) | "cashflow" (YTD-diff).
    """
    grid: dict[tuple[str, str], float | None] = {}
    for c in concepts:
        for p in periods:
            if kind == "instant":
                v, _ = instant_value(facts, c, p.end)
            elif kind == "cashflow":
                v, _ = cashflow_value(facts, c, p)
            else:
                v, _ = quarterly_value(facts, c, p)
            grid[(c, p.label)] = v
    return grid


# ===========================================================================
# 7. Integrity checks
# ===========================================================================

SMALL_MAGNITUDE = 1_000.0   # below this, values are per-share / ratios, not dollars
SMALL_ABS_TOL = 0.01        # one cent


def _ok(lhs: float, rhs: float) -> tuple[bool, float]:
    """Pass if within EITHER the absolute or the relative band."""
    diff = lhs - rhs
    scale = max(abs(lhs), abs(rhs), 1.0)
    abs_tol = SMALL_ABS_TOL if scale < SMALL_MAGNITUDE else ABS_TOL
    return (abs(diff) <= abs_tol or abs(diff) / scale <= REL_TOL), diff


def _s(x: float | None) -> float:
    return 0.0 if x is None else x


def run_checks(facts: dict[str, list[Fact]], periods: list[Period]) -> pd.DataFrame:
    inst = value_grid(
        facts,
        periods,
        [
            "Assets", "AssetsCurrent", "Liabilities", "LiabilitiesCurrent",
            "LiabilitiesAndStockholdersEquity", "StockholdersEquity",
            "CashAndCashEquivalentsAtCarryingValue", "DebtSecuritiesCurrent",
            "EquitySecuritiesFvNi", "AccountsReceivableNetCurrent", "InventoryNet",
            "PrepaidExpenseAndOtherAssetsCurrent", "AccountsPayableCurrent",
            "AccruedLiabilitiesCurrent", "DebtCurrent", "CommonStockValue",
            "PreferredStockValueOutstanding", "AdditionalPaidInCapital",
            "AccumulatedOtherComprehensiveIncomeLossNetOfTax",
            "RetainedEarningsAccumulatedDeficit",
            # cash flow ending/beginning balance concept
            BALANCE_CASH,
        ],
        "instant",
    )
    dur = value_grid(
        facts,
        periods,
        [
            "Revenues", "CostOfRevenue", "GrossProfit", "ResearchAndDevelopmentExpense",
            "SellingGeneralAndAdministrativeExpense", "OperatingExpenses",
            "OperatingIncomeLoss", "InvestmentIncomeInterest", "InterestExpenseNonoperating",
            "OtherNonoperatingIncomeExpense", "NonoperatingIncomeExpense",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeTaxExpenseBenefit", "NetIncomeLoss",
            "OtherComprehensiveIncomeLossNetOfTaxPortionAttributableToParent",
            "ComprehensiveIncomeNetOfTax", "StockIssuedDuringPeriodValueNewIssues",
            "ShareBasedCompensation",
            "PaymentsRelatedToTaxWithholdingForShareBasedCompensation",
            "PaymentsForRepurchaseOfCommonStock", "PaymentsOfDividendsCommonStock",
        ],
        "duration",
    )
    # Discrete-quarter cash-flow figures (YTD-differenced) for the CF checks.
    cf = value_grid(
        facts,
        periods,
        [
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInInvestingActivities",
            "NetCashProvidedByUsedInFinancingActivities",
            "EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
            "NetIncomeLoss",
        ],
        "cashflow",
    )

    prior_se = {p.label: inst.get(("StockholdersEquity", pl.label)) if (pl := _prior(periods, p)) else None
                for p in periods}

    out = []

    def add(pid, period, name, lhs, rhs, detail="", inputs_present=True):
        if lhs is None or rhs is None or not inputs_present:
            out.append(dict(check_id=pid, period=period.label, period_end=period.end,
                            check=name, lhs=lhs, rhs=rhs, difference=None,
                            status="SKIP (missing inputs)", detail=detail))
            return
        good, diff = _ok(lhs, rhs)
        out.append(dict(check_id=pid, period=period.label, period_end=period.end,
                        check=name, lhs=lhs, rhs=rhs, difference=diff,
                        status="PASS" if good else "FAIL", detail=detail))

    for p in periods:
        L = p.label
        g = lambda c: inst.get((c, L))
        d = lambda c: dur.get((c, L))
        cfv = lambda c: cf.get((c, L))

        # --- Balance sheet ------------------------------------------------
        add("BS01", p, "Assets = Liabilities and Shareholders' Equity",
            g("Assets"), g("LiabilitiesAndStockholdersEquity"))

        add("BS02", p, "Assets = Total Liabilities + Total Shareholders' Equity",
            g("Assets"),
            None if g("Liabilities") is None or g("StockholdersEquity") is None
            else g("Liabilities") + g("StockholdersEquity"))

        ca_parts = ["CashAndCashEquivalentsAtCarryingValue", "DebtSecuritiesCurrent",
                    "EquitySecuritiesFvNi", "AccountsReceivableNetCurrent", "InventoryNet",
                    "PrepaidExpenseAndOtherAssetsCurrent"]
        ca_have = [g(c) for c in ca_parts if g(c) is not None]
        add("BS03", p, "Total current assets = sum of components",
            g("AssetsCurrent"), sum(ca_have) if ca_have else None,
            detail=f"{len(ca_have)}/{len(ca_parts)} components tagged")

        cl_parts = ["AccountsPayableCurrent", "AccruedLiabilitiesCurrent", "DebtCurrent"]
        cl_have = [g(c) for c in cl_parts if g(c) is not None]
        add("BS04", p, "Total current liabilities = sum of components",
            g("LiabilitiesCurrent"), sum(cl_have) if cl_have else None,
            detail=f"{len(cl_have)}/{len(cl_parts)} components tagged")

        eq_parts = ["PreferredStockValueOutstanding", "CommonStockValue", "AdditionalPaidInCapital",
                    "AccumulatedOtherComprehensiveIncomeLossNetOfTax",
                    "RetainedEarningsAccumulatedDeficit"]
        eq_have = [g(c) for c in eq_parts if g(c) is not None]
        add("BS05", p, "Total shareholders' equity = sum of components",
            g("StockholdersEquity"), sum(eq_have) if eq_have else None,
            detail=f"{len(eq_have)}/{len(eq_parts)} components tagged")

        # --- Income statement ---------------------------------------------
        add("IS01", p, "Revenue - Cost of revenue = Gross profit",
            None if d("Revenues") is None or d("CostOfRevenue") is None
            else d("Revenues") - d("CostOfRevenue"), d("GrossProfit"))

        add("IS02", p, "Gross profit - Total opex = Operating income",
            None if d("GrossProfit") is None or d("OperatingExpenses") is None
            else d("GrossProfit") - d("OperatingExpenses"), d("OperatingIncomeLoss"))

        add("IS03", p, "Operating income + Total other income = Pretax income",
            None if d("OperatingIncomeLoss") is None or d("NonoperatingIncomeExpense") is None
            else d("OperatingIncomeLoss") + d("NonoperatingIncomeExpense"),
            d("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"))

        add("IS04", p, "Pretax income - Tax expense = Net income",
            None if d("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest") is None
            or d("IncomeTaxExpenseBenefit") is None
            else d("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest")
                 - d("IncomeTaxExpenseBenefit"), d("NetIncomeLoss"))

        add("IS05", p, "Net income + OCI = Comprehensive income",
            None if d("NetIncomeLoss") is None
            or d("OtherComprehensiveIncomeLossNetOfTaxPortionAttributableToParent") is None
            else d("NetIncomeLoss") + d("OtherComprehensiveIncomeLossNetOfTaxPortionAttributableToParent"),
            d("ComprehensiveIncomeNetOfTax"))

        # --- Equity rollforward -------------------------------------------
        beg = prior_se[L]
        end_se = g("StockholdersEquity")
        if beg is None or end_se is None:
            add("EQ01", p, "Shareholders' equity rollforward", None, None,
                detail="no prior-period equity balance (first period in panel)")
        else:
            ni   = _s(d("NetIncomeLoss"))
            oci  = _s(d("OtherComprehensiveIncomeLossNetOfTaxPortionAttributableToParent"))
            iss  = _s(d("StockIssuedDuringPeriodValueNewIssues"))
            sbc  = _s(d("ShareBasedCompensation"))
            twh  = _s(d("PaymentsRelatedToTaxWithholdingForShareBasedCompensation"))
            bb   = _s(d("PaymentsForRepurchaseOfCommonStock"))
            div  = _s(d("PaymentsOfDividendsCommonStock"))
            roll = beg + ni + oci + iss + sbc - twh - bb - div
            missing = [n for n, v in [
                ("NetIncomeLoss", d("NetIncomeLoss")),
                ("OCI", d("OtherComprehensiveIncomeLossNetOfTaxPortionAttributableToParent")),
                ("StockIssued", d("StockIssuedDuringPeriodValueNewIssues")),
                ("SBC", d("ShareBasedCompensation")),
                ("TaxWithholding", d("PaymentsRelatedToTaxWithholdingForShareBasedCompensation")),
                ("Repurchases", d("PaymentsForRepurchaseOfCommonStock")),
                ("Dividends", d("PaymentsOfDividendsCommonStock")),
            ] if v is None]
            det = (f"beg={beg:,.0f} NI={ni:,.0f} OCI={oci:,.0f} iss={iss:,.0f} "
                   f"SBC={sbc:,.0f} twh=({twh:,.0f}) bb=({bb:,.0f}) div=({div:,.0f})")
            if missing:
                det += f" | untagged, treated as 0: {', '.join(missing)}"
            add("EQ01", p, "Shareholders' equity rollforward", roll, end_se, detail=det)

        # --- Cash flow statement ------------------------------------------
        op  = cfv("NetCashProvidedByUsedInOperatingActivities")
        inv = cfv("NetCashProvidedByUsedInInvestingActivities")
        fin = cfv("NetCashProvidedByUsedInFinancingActivities")
        fx  = cfv("EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")
        chg = cfv("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect")

        # CF01: the three sections (plus any separately reported FX effect) sum
        # to the net change in cash. FX is optional and defaults to 0.
        add("CF01", p, "Operating + Investing + Financing (+FX) = Change in cash",
            None if op is None or inv is None or fin is None
            else op + inv + fin + _s(fx),
            chg,
            detail="FX effect treated as 0 when untagged" if fx is None else "")

        # CF02: cash rollforward. Beginning cash = prior period-end balance;
        # ending cash = current period-end balance.
        end_cash = g(BALANCE_CASH)
        beg_cash = instant_value(facts, BALANCE_CASH, p.prior_end)[0] if p.prior_end else None
        if beg_cash is None or end_cash is None or chg is None:
            add("CF02", p, "Beginning cash + Change in cash = Ending cash",
                None, None,
                detail="no prior-period cash balance (first period in panel)"
                       if p.prior_end is None else "missing cash balance/change")
        else:
            add("CF02", p, "Beginning cash + Change in cash = Ending cash",
                beg_cash + chg, end_cash,
                detail=f"beg={beg_cash:,.0f} chg={chg:,.0f}")

        # CF03: SCF ending cash ties to the balance-sheet cash line. The SCF
        # balance includes restricted cash; the BS line may exclude it, so a
        # small residual here usually means restricted cash, not an error.
        bs_cash = g("CashAndCashEquivalentsAtCarryingValue")
        add("CF03", p, "Ending cash (SCF) = Cash on balance sheet",
            end_cash, bs_cash,
            detail="SCF balance includes restricted cash; BS line may exclude it")

        # CF04: net income reconciles across the income and cash flow statements.
        add("CF04", p, "Net income (SCF) = Net income (income statement)",
            cfv("NetIncomeLoss"), d("NetIncomeLoss"))

    df = pd.DataFrame(out)
    return df


def _prior(periods: list[Period], p: Period) -> Period | None:
    for i, q in enumerate(periods):
        if q.end == p.end and i > 0:
            return periods[i - 1]
    return None


# ===========================================================================
# 8. Long-format export (Sheets / BI friendly)
# ===========================================================================

def long_table(statements: list[tuple[str, pd.DataFrame]], periods: list[Period]) -> pd.DataFrame:
    frames = []
    for stmt, df in statements:
        if df is None or df.empty:
            continue
        m = df.melt(id_vars=["FSLI", "XBRL Tag"], var_name="period", value_name="value")
        m["statement"] = stmt
        frames.append(m)
    out = pd.concat(frames, ignore_index=True)
    ends = {p.label: p.end for p in periods}
    out["period_end"] = out["period"].map(ends)
    return out[["statement", "FSLI", "XBRL Tag", "period", "period_end", "value"]]


# ===========================================================================
# 9. CLI
# ===========================================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cik", type=int, default=1045810, help="CIK, digits only (NVIDIA = 1045810)")
    ap.add_argument("--ticker", default="NVDA")
    ap.add_argument("--bs", required=True, help="balance sheet mapping CSV")
    ap.add_argument("--is", dest="is_", required=True, help="income statement mapping CSV")
    ap.add_argument("--scf", default="", help="cash flow statement mapping CSV (optional)")
    ap.add_argument("--user-agent", default="", help='required by SEC, e.g. "Jane Doe jane@firm.com"')
    ap.add_argument("--periods", type=int, default=12, help="most recent N quarters (0 = all)")
    ap.add_argument("--outdir", default="./out")
    ap.add_argument("--cache", default="", help="path to cache the companyfacts JSON")
    ap.add_argument("--fixture", default="", help="local companyfacts JSON; skips network")
    args = ap.parse_args(argv)

    os.makedirs(args.outdir, exist_ok=True)

    if args.fixture:
        with open(args.fixture) as fh:
            cf = json.load(fh)
    else:
        if not args.user_agent:
            print("ERROR: --user-agent is mandatory. SEC returns 403 without a real "
                  'contact string, e.g. --user-agent "Jane Doe jane@firm.com"', file=sys.stderr)
            return 2
        cf = fetch_companyfacts(args.cik, args.user_agent, args.cache or None)

    facts = index_facts(cf)
    periods = build_periods(facts, args.periods)
    print(f"Entity: {cf.get('entityName','?')}  |  periods: "
          f"{periods[0].label} ({periods[0].end}) -> {periods[-1].label} ({periods[-1].end})")

    bs_lines = load_mapping(args.bs, "BS")
    is_lines = load_mapping(args.is_, "IS")
    scf_lines = load_mapping(args.scf, "SCF") if args.scf else []
    msg = f"Mapping: {len(bs_lines)} BS lines, {len(is_lines)} IS lines"
    if scf_lines:
        msg += f", {len(scf_lines)} SCF lines"
    print(msg)

    panel_bs, prov_bs = build_panel(facts, bs_lines, periods)
    panel_is, prov_is = build_panel(facts, is_lines, periods)
    panel_scf, prov_scf = (build_panel(facts, scf_lines, periods)
                           if scf_lines else (pd.DataFrame(), pd.DataFrame()))
    checks = run_checks(facts, periods)

    written = []
    panel_bs.to_csv(f"{args.outdir}/panel_balance_sheet.csv", index=False)
    written.append("panel_balance_sheet.csv")
    panel_is.to_csv(f"{args.outdir}/panel_income_statement.csv", index=False)
    written.append("panel_income_statement.csv")
    if scf_lines:
        panel_scf.to_csv(f"{args.outdir}/panel_cash_flow.csv", index=False)
        written.append("panel_cash_flow.csv")

    prov_frames = [prov_bs.assign(statement="BS"), prov_is.assign(statement="IS")]
    if scf_lines:
        prov_frames.append(prov_scf.assign(statement="SCF"))
    pd.concat(prov_frames).to_csv(f"{args.outdir}/tag_provenance.csv", index=False)
    written.append("tag_provenance.csv")

    checks.to_csv(f"{args.outdir}/check_results.csv", index=False)
    written.append("check_results.csv")

    statements = [("Balance Sheet", panel_bs), ("Income Statement", panel_is)]
    if scf_lines:
        statements.append(("Cash Flow", panel_scf))
    long_table(statements, periods).to_csv(f"{args.outdir}/facts_long.csv", index=False)
    written.append("facts_long.csv")

    # coverage
    pcols = [p.label for p in periods]
    panels = [panel_bs, panel_is] + ([panel_scf] if scf_lines else [])
    filled = sum(pnl[pcols].notna().sum().sum() for pnl in panels)
    total = sum(len(pnl) for pnl in panels) * len(pcols)
    print(f"Coverage: {filled}/{total} cells populated ({filled/total:.0%})")

    n_fail = (checks.status == "FAIL").sum()
    n_skip = checks.status.str.startswith("SKIP").sum()
    n_pass = (checks.status == "PASS").sum()
    print(f"Checks: {n_pass} pass, {n_fail} FAIL, {n_skip} skipped")
    if n_fail:
        print("\n--- FAILURES ---")
        cols = ["check_id", "period", "check", "lhs", "rhs", "difference", "detail"]
        print(checks[checks.status == "FAIL"][cols].to_string(index=False))
    print(f"\nWrote {len(written)} files to {args.outdir}/")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
