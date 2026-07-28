#!/usr/bin/env python3
"""
segment_extension.py — dimensional (axis/member) XBRL extraction for NVIDIA.

WHY THIS IS NOT A COMPANY FACTS PULL
------------------------------------
The Company Facts API cannot serve segment, geographic, or product revenue.
Its payload shape is:

    facts -> {taxonomy} -> {concept} -> units -> {unit} -> [
        {start, end, val, accn, fy, fp, form, filed, frame}, ...
    ]

There is no slot for a dimension. The SEC's extractor populates that JSON from
facts reported in the DEFAULT context only — undimensioned, entity-wide values.
A fact carrying ConsolidationItemsAxis=OperatingSegmentsMember and
StatementBusinessSegmentsAxis=ComputeAndNetworkingMember is dimensioned, so it
is simply absent. Same for companyconcept and frames: same extractor, same rule.

Dimensional facts exist only in the filing's own XBRL instance. For inline-XBRL
filers (NVIDIA since FY2020) EDGAR generates an extracted instance alongside the
document:

    https://www.sec.gov/Archives/edgar/data/1045810/{accession}/nvda-{date}_htm.xml

That file has explicit <xbrli:context> elements whose <xbrldi:explicitMember>
children carry the axis/member pairs. This module resolves contexts to
dimensions, joins them onto the facts, and pivots the three analyses.

WHAT THIS MEANS FOR THE PIPELINE
--------------------------------
xbrl_panel.py stays as it is. It is the right tool for consolidated statements
and its check battery depends on undimensioned facts. This runs beside it on a
different source. Do not try to merge them: a dimensional revenue fact and a
consolidated revenue fact are different numbers and summing them double-counts.

ALIGNED HISTORICAL MODE  (--periods N)
--------------------------------------
A single instance carries only one quarter, so the default single-filing mode
cannot produce a history. --periods N reuses xbrl_panel.py's OWN period logic
(imported directly, so the grids are identical by construction) to build the
last N fiscal quarters, then fetches every filing instance needed to cover them
and pivots revenue-by-dimension onto that same FY####Q# column grid. Fiscal Q4
is derived as FY less the three prior quarters, exactly as the panel does — so
the segment/platform/geographic panels line up column-for-column with
panel_income_statement.csv.

USAGE  (single-line commands -- do not copy a line continuation)
-----
  Aligned history matching the panel's 12 quarters (PRIMARY: market platform):
    python segment_extension.py --periods 12 --user-agent "Ozoemena Nnamadim ozoemena@otnnamadim.com"

  Aligned history, offline (grid from a companyfacts JSON, instances from a folder):
    python segment_extension.py --periods 12 --companyfacts companyfacts.json --instance-dir ./instances

  Latest 10-Q, accession resolved automatically (single-filing mode):
    python segment_extension.py --latest 10-Q --user-agent "Ozoemena Nnamadim ozoemena@otnnamadim.com"

  A specific filing:
    python segment_extension.py --accession 0001045810-26-000052 --user-agent "Ozoemena Nnamadim ozoemena@otnnamadim.com"

  Offline, against an instance already on disk:
    python segment_extension.py --instance nvda-20260426_htm.xml

  NVIDIA accessions:
    0001045810-26-000052   Q1 FY2027 10-Q  (new platform members: Hyperscale / ACIE / Edge Computing)
    0001045810-26-000021   FY2026 10-K     (old members: Data Center / Gaming / ProViz / Auto / OEM)

  SEC wants a descriptive User-Agent with a name AND an email. A bare email is
  frequently rejected with a 403.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime

import pandas as pd

try:
    import requests
except ImportError:
    requests = None

# Aligned mode reuses xbrl_panel.py's period logic so the two grids are the SAME
# by construction (same fact index, same build_periods, same Q4 derivation) —
# rather than a re-implementation that could drift. It must be importable
# (placed beside this script) for --periods to work.
try:
    import xbrl_panel as _panel
except Exception:  # noqa: BLE001 — any import failure downgrades gracefully
    _panel = None

XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"

ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


def latest_accession(cik: int, form: str, user_agent: str) -> str:
    """Most recent accession for a given form type."""
    if requests is None:
        raise RuntimeError("requests not installed")
    hdr = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate",
           "Host": "data.sec.gov"}
    r = requests.get(SUBMISSIONS.format(cik=cik), headers=hdr, timeout=30)
    r.raise_for_status()
    recent = r.json()["filings"]["recent"]
    for accn, ftype, fdate in zip(recent["accessionNumber"], recent["form"],
                                  recent["filingDate"]):
        if ftype == form:
            print(f"Resolved latest {form}: {accn} (filed {fdate})")
            return accn
    raise RuntimeError(f"no {form} in the recent filings index for CIK {cik}")


# ===========================================================================
# 1. Instance retrieval
# ===========================================================================

def fetch_instance(cik: int, accession: str, user_agent: str) -> bytes:
    """Locate and download the extracted instance for an inline-XBRL filing."""
    if requests is None:
        raise RuntimeError("requests not installed; use --instance for offline mode")
    accn = accession.replace("-", "")
    base = ARCHIVES.format(cik=cik, accn=accn)
    hdr = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}

    idx = None
    for attempt in range(4):
        idx = requests.get(base + "index.json", headers=hdr, timeout=30)
        if idx.status_code == 200:
            break
        if idx.status_code in (403, 429, 503):
            time.sleep(2 ** attempt)
            continue
        idx.raise_for_status()
    if idx is None or idx.status_code != 200:
        raise RuntimeError(
            f"SEC returned {idx.status_code} for {base}index.json. A 403 almost always "
            f"means the User-Agent was rejected -- it needs a name and an email, e.g. "
            f'--user-agent "Jane Doe jane@firm.com". A 404 means the accession is wrong.')
    names = [i["name"] for i in idx.json()["directory"]["item"]]

    # EDGAR names the extracted instance <primary-doc-stem>_htm.xml
    cands = [n for n in names if n.endswith("_htm.xml")]
    if not cands:
        raise RuntimeError(
            f"No *_htm.xml extracted instance in {base}. Files present: {names[:20]}. "
            f"Pre-inline-XBRL filings carry a plain .xml instance instead.")
    resp = requests.get(base + cands[0], headers=hdr, timeout=60)
    resp.raise_for_status()
    print(f"Instance: {cands[0]}  ({len(resp.content):,} bytes)")
    return resp.content


# ===========================================================================
# 1b. Aligned-mode retrieval: period grid + multiple filing instances
# ===========================================================================

def build_period_grid(cik: int, user_agent: str, n: int,
                      companyfacts_path: str | None = None) -> list:
    """Return the SAME Period objects xbrl_panel.py would build for --periods N.

    We import and call the panel's own index_facts / build_periods so the
    two panels share one definition of "the last N quarters" (identical end
    dates, FY####Q# labels, and prior_q_ends for Q4 derivation)."""
    if _panel is None:
        raise RuntimeError(
            "xbrl_panel.py must be importable to align periods. Put it next to this "
            "script; --periods reuses its period logic so the grids match exactly.")
    if companyfacts_path:
        with open(companyfacts_path) as fh:
            cf = json.load(fh)
    elif user_agent:
        cf = _panel.fetch_companyfacts(cik, user_agent)
    else:
        raise RuntimeError(
            "aligned mode needs --user-agent (online) or --companyfacts (offline) "
            "to build the period grid.")
    facts = _panel.index_facts(cf)
    return _panel.build_periods(facts, n)


def resolve_filings(cik: int, user_agent: str, earliest) -> list[tuple[str, str, "datetime.date"]]:
    """All 10-Q / 10-K accessions whose reportDate is on or after `earliest`,
    oldest first. reportDate equals the fiscal period-end the filing reports,
    so this is exactly the set of instances needed to cover the grid (the
    10-Ks supply each fiscal Q4's annual figure for derivation)."""
    if requests is None:
        raise RuntimeError("requests not installed; use --instance-dir for offline aligned mode")
    hdr = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}
    r = requests.get(SUBMISSIONS.format(cik=cik), headers=hdr, timeout=30)
    r.raise_for_status()
    recent = r.json()["filings"]["recent"]
    keep = []
    for accn, form, rdate in zip(recent["accessionNumber"], recent["form"],
                                 recent["reportDate"]):
        if form not in ("10-Q", "10-K", "10-Q/A", "10-K/A"):
            continue
        try:
            rd = datetime.strptime(rdate, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if rd >= earliest:
            keep.append((accn, form, rd))
    keep.sort(key=lambda t: t[2])
    return keep


def gather_facts(cik: int, filings: list, user_agent: str) -> pd.DataFrame:
    """Fetch and parse every filing instance, concatenated into one fact table.

    A quarter's ~90-day dimensioned fact shows up both in its own filing (as the
    current quarter) and in the next year's filing (as the prior-year
    comparative); the lookups below take the last matching value, and the two are
    equal, so the duplication is harmless."""
    frames = []
    for accn, form, rd in filings:
        try:
            content = fetch_instance(cik, accn, user_agent)
        except Exception as exc:  # noqa: BLE001 — skip a bad filing, keep the rest
            print(f"  skip {accn} ({form} {rd}): {exc}", file=sys.stderr)
            continue
        fdf = parse_facts(content)
        if not fdf.empty:
            fdf["source_accession"] = accn
            fdf["source_form"] = form
            frames.append(fdf)
        time.sleep(0.15)  # stay under EDGAR's ~10 req/sec
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def gather_facts_offline(instance_dir: str) -> pd.DataFrame:
    """Aligned mode without the network: parse every *_htm.xml in a folder."""
    paths = sorted(glob.glob(os.path.join(instance_dir, "*_htm.xml"))) or \
        sorted(glob.glob(os.path.join(instance_dir, "*.xml")))
    frames = []
    for path in paths:
        with open(path, "rb") as fh:
            fdf = parse_facts(fh.read())
        if not fdf.empty:
            fdf["source_accession"] = os.path.basename(path)
            frames.append(fdf)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ===========================================================================
# 2. Context resolution — the part Company Facts throws away
# ===========================================================================

def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _qname(elem_text: str, nsmap: dict[str, str]) -> str:
    """Return 'prefix:LocalName' for a QName written in element text."""
    return elem_text.strip()


def parse_contexts(root: ET.Element) -> dict[str, dict]:
    """contextRef -> {start, end, instant, dims: {axis: member}}"""
    out: dict[str, dict] = {}
    for ctx in root.iter(f"{{{XBRLI}}}context"):
        cid = ctx.get("id")
        rec: dict = {"start": None, "end": None, "instant": None, "dims": {}}

        period = ctx.find(f"{{{XBRLI}}}period")
        if period is not None:
            for child in period:
                ln = _localname(child.tag)
                if ln == "startDate":
                    rec["start"] = child.text.strip()
                elif ln == "endDate":
                    rec["end"] = child.text.strip()
                elif ln == "instant":
                    rec["instant"] = child.text.strip()

        # Dimensions live under entity/segment (and occasionally scenario).
        for holder in ("segment", "scenario"):
            for seg in ctx.iter(f"{{{XBRLI}}}{holder}"):
                for m in seg.iter(f"{{{XBRLDI}}}explicitMember"):
                    axis = m.get("dimension")
                    if axis and m.text:
                        rec["dims"][axis.strip()] = m.text.strip()
        out[cid] = rec
    return out


def parse_facts(content: bytes) -> pd.DataFrame:
    """Flatten an XBRL instance into one row per fact, with dimensions joined."""
    root = ET.fromstring(content)
    contexts = parse_contexts(root)

    units: dict[str, str] = {}
    for u in root.iter(f"{{{XBRLI}}}unit"):
        measures = [m.text.strip() for m in u.iter(f"{{{XBRLI}}}measure") if m.text]
        units[u.get("id")] = "/".join(measures) if measures else ""

    rows = []
    for el in root:
        ctxref = el.get("contextRef")
        if ctxref is None or ctxref not in contexts:
            continue
        if el.text is None or not el.text.strip():
            continue
        raw = el.text.strip().replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue  # non-numeric fact (text block, etc.)
        if el.get("sign") == "-":
            val = -val

        ctx = contexts[ctxref]
        tag = el.tag
        if "}" in tag:
            ns, local = tag[1:].split("}", 1)
        else:
            ns, local = "", tag
        rows.append({
            "concept": local,
            "namespace": ns,
            "value": val,
            "unit": units.get(el.get("unitRef", ""), ""),
            "decimals": el.get("decimals"),
            "start": ctx["start"],
            "end": ctx["end"] or ctx["instant"],
            "is_instant": ctx["instant"] is not None,
            "context": ctxref,
            "dims": ctx["dims"],
            "n_dims": len(ctx["dims"]),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["days"] = None
    mask = df["start"].notna() & df["end"].notna()
    df.loc[mask, "days"] = (
        pd.to_datetime(df.loc[mask, "end"]) - pd.to_datetime(df.loc[mask, "start"])
    ).dt.days
    return df


def explode_axes(df: pd.DataFrame) -> pd.DataFrame:
    """One column per axis encountered, so facts can be filtered by dimension."""
    if df.empty:
        return df
    axes = sorted({a for d in df["dims"] for a in d})
    out = df.copy()
    for a in axes:
        out[a] = out["dims"].map(lambda d, a=a: d.get(a))
    return out


# ===========================================================================
# 3. Axis / member resolution
#
# Members are NOT hardcoded. NVIDIA renamed its platform members in Q1 FY2027
# and a hardcoded list would silently return an empty frame. These are regex
# probes against whatever members the instance actually declares, with the
# resolved names printed so a rename is visible rather than silent.
# ===========================================================================

AXIS_PROBES = {
    "consolidation": r"ConsolidationItemsAxis$",
    "segment":       r"(StatementBusinessSegmentsAxis|SegmentReportingAxis)$",
    "geography":     r"StatementGeographicalAxis$",
    "product":       r"(ProductOrServiceAxis|ProductAndServiceAxis)$",
}

REVENUE_CONCEPTS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
]
SEGMENT_CONCEPTS = {
    "revenue": REVENUE_CONCEPTS,
    "other_items": ["SegmentReportingOtherItemAmount"],
    "operating_income": ["OperatingIncomeLoss"],
}


def resolve_axes(df: pd.DataFrame) -> dict[str, str | None]:
    axes = sorted({a for d in df["dims"] for a in d})
    found = {}
    for key, pat in AXIS_PROBES.items():
        hit = next((a for a in axes if re.search(pat, a)), None)
        found[key] = hit
    return found


def pick_concept(df: pd.DataFrame, candidates: list[str]) -> str | None:
    present = set(df["concept"])
    return next((c for c in candidates if c in present), None)


def _annual_or_quarter(df: pd.DataFrame, want: str) -> pd.DataFrame:
    if "days" not in df.columns:
        return df
    d = df[df["days"].notna()].copy()
    if want == "quarter":
        return d[(d["days"] >= 80) & (d["days"] <= 100)]
    return d[(d["days"] >= 350) & (d["days"] <= 380)]


def _pivot(df: pd.DataFrame, axis: str, label: str) -> pd.DataFrame:
    if df.empty or axis not in df.columns:
        return pd.DataFrame()
    d = df[df[axis].notna()].copy()
    if d.empty:
        return pd.DataFrame()
    d["member"] = d[axis].str.split(":").str[-1].str.replace("Member$", "", regex=True)
    p = d.pivot_table(index="member", columns="end", values="value", aggfunc="last")
    p = (p / 1e6).round(1)
    p.index.name = f"{label} ($mm)"
    return p


# ===========================================================================
# 3b. Aligned quarterly panel — same period grid as xbrl_panel.py
# ===========================================================================

QTR_DAYS = (80, 100)
YTD_DAYS = (350, 380)


def aligned_quarterly_revenue(fac: pd.DataFrame, periods: list, axis: str | None,
                              label: str, cons_axis: str | None = None,
                              operating_only: bool = False) -> pd.DataFrame:
    """Pivot dimensioned revenue onto the panel's FY####Q# columns.

    For each member and each period we take the natively reported ~90-day fact.
    Fiscal Q4 is never filed on a 10-Q, so — exactly like xbrl_panel.py's
    quarterly_value — it is derived as the FY figure less the three prior
    quarters (period.prior_q_ends, which the panel already attached). A member
    that did not exist in a given quarter (e.g. post-rename platform names in
    older periods) is left blank, so old and new taxonomies coexist in one
    panel without being force-mapped onto each other."""
    if axis is None or axis not in fac.columns:
        return pd.DataFrame()

    d = fac.copy()
    d["days_num"] = pd.to_numeric(d["days"], errors="coerce")
    if operating_only and cons_axis and cons_axis in d.columns:
        # keep operating-segment facts only; drop elimination / reconciling members
        d = d[d[cons_axis].isna() | d[cons_axis].str.contains("OperatingSegments", na=False)]

    members = sorted(m for m in d[axis].dropna().unique())
    if not members:
        return pd.DataFrame()

    def val(member: str, end_iso: str, lo: int, hi: int) -> float | None:
        sub = d[(d[axis] == member) & (d["end"] == end_iso)
                & (d["days_num"] >= lo) & (d["days_num"] <= hi)]
        for c in REVENUE_CONCEPTS:            # ASC 606 concept first, then fallbacks
            s = sub[sub["concept"] == c]["value"]
            if len(s):
                return float(s.iloc[-1])
        return None

    cols: dict[str, dict] = {}
    for p in periods:
        end_iso = p.end.isoformat()
        col = {}
        for m in members:
            if p.fp != "Q4":
                v = val(m, end_iso, *QTR_DAYS)
            else:
                fy_v = val(m, end_iso, *YTD_DAYS)
                if fy_v is None:
                    v = None
                else:
                    total, ok = 0.0, True
                    for qe in p.prior_q_ends:
                        qv = val(m, qe.isoformat(), *QTR_DAYS)
                        if qv is None:
                            ok = False
                            break
                        total += qv
                    v = fy_v - total if ok else None
            col[m] = v
        cols[p.label] = col

    df = pd.DataFrame(cols)
    df.index = [re.sub(r"Member$", "", str(m).split(":")[-1]) for m in df.index]
    df.index.name = f"{label} ($mm)"
    df = (df / 1e6).round(1)
    return df.dropna(how="all")   # drop members with no data across the window


# ===========================================================================
# 4. The three analyses  (single-filing mode)
# ===========================================================================

def segment_analysis(df: pd.DataFrame, axes: dict, period: str = "annual"):
    seg_axis, cons_axis = axes.get("segment"), axes.get("consolidation")
    if seg_axis is None:
        return {}, "no business-segment axis in this instance"

    d = _annual_or_quarter(df, "annual" if period == "annual" else "quarter")
    if cons_axis and cons_axis in d.columns:
        # Operating-segment facts only; drops intersegment-elimination and
        # reconciling-item members that would otherwise be summed in.
        d = d[d[cons_axis].isna() | d[cons_axis].str.contains("OperatingSegments", na=False)]

    out = {}
    for key, cands in SEGMENT_CONCEPTS.items():
        concept = pick_concept(d, cands)
        if concept is None:
            out[key] = pd.DataFrame()
            continue
        out[key] = _pivot(d[d["concept"] == concept], seg_axis, key)

    note = ("Segment operating income does NOT equal consolidated EBIT. Unallocated corporate "
            "costs sit outside the segment note: Q1FY27 segment OI totalled 56,276 on 81,615 "
            "of revenue (69.0%), far above the consolidated margin. Pull the reconciling-item "
            "facts before using any of this as an EBIT margin input.")
    rev, oi = out.get("revenue"), out.get("operating_income")
    if isinstance(rev, pd.DataFrame) and isinstance(oi, pd.DataFrame) \
            and not rev.empty and not oi.empty:
        common = rev.index.intersection(oi.index)
        cols = rev.columns.intersection(oi.columns)
        if len(common) and len(cols):
            out["operating_margin"] = (oi.loc[common, cols] / rev.loc[common, cols]).round(4)
    return out, note


def geographic_analysis(df: pd.DataFrame, axes: dict, period: str = "annual"):
    geo = axes.get("geography")
    if geo is None:
        return pd.DataFrame(), "no geographical axis in this instance"
    d = _annual_or_quarter(df, "annual" if period == "annual" else "quarter")
    concept = pick_concept(d, REVENUE_CONCEPTS)
    if concept is None:
        return pd.DataFrame(), "no revenue concept found"
    p = _pivot(d[d["concept"] == concept], geo, "Geography")
    note = ("As of the Q1 FY2027 10-Q the caption reads 'Geographic Revenue based upon "
            "Customer Headquarters Location'. Earlier filings used BILLING location, under "
            "which Singapore was a top-three line. On the customer-HQ basis Singapore does "
            "not appear and the US share jumps (58.3% -> 78.1% of revenue Q1FY26 -> Q1FY27). "
            "Check the caption before splicing any pre-FY27 geographic series.")
    return p, note


def platform_analysis(df: pd.DataFrame, axes: dict, period: str = "quarter"):
    prod = axes.get("product")
    if prod is None:
        return pd.DataFrame(), "no product/service axis in this instance"
    d = _annual_or_quarter(df, "annual" if period == "annual" else "quarter")
    concept = pick_concept(d, REVENUE_CONCEPTS)
    if concept is None:
        return pd.DataFrame(), "no revenue concept found"
    p = _pivot(d[d["concept"] == concept], prod, "Platform")
    note = ("Members changed at Q1 FY2027: pre-FY27 instances declare Data Center / Gaming / "
            "Professional Visualization / Automotive / OEM & Other; FY27+ declare Data Center "
            "(Hyperscale + ACIE) and Edge Computing. This is the ASC 606 revenue "
            "disaggregation ONLY. The ASC 280 operating segments (Compute & Networking / "
            "Graphics) are UNCHANGED and still reported. The two are different partitions: "
            "Q1FY27 Data Center 75,246 vs C&N 74,550, offsetting by 696. Do not map one onto "
            "the other.")
    return p, note


def coverage_report(df: pd.DataFrame, axes: dict) -> None:
    print("\nAxes declared in this instance:")
    all_axes = sorted({a for d in df["dims"] for a in d})
    for a in all_axes:
        members = sorted({d[a].split(":")[-1] for d in df["dims"] if a in d})
        role = next((k for k, v in axes.items() if v == a), "")
        flag = f"  <- resolved as '{role}'" if role else ""
        print(f"  {a.split(':')[-1]}{flag}")
        for m in members[:12]:
            print(f"      - {m}")
        if len(members) > 12:
            print(f"      ... {len(members)-12} more")


# ===========================================================================
# 5. CLI
# ===========================================================================

def run_aligned(args) -> int:
    """--periods N: historical panels on the SAME grid as xbrl_panel.py."""
    periods = build_period_grid(args.cik, args.user_agent, args.periods,
                                args.companyfacts or None)
    print(f"Aligned to panel periods: {periods[0].label} ({periods[0].end}) -> "
          f"{periods[-1].label} ({periods[-1].end})")

    # every end date the panel needs, INCLUDING the prior quarters a Q4
    # derivation reaches back into, so the fetch window covers them.
    needed = set()
    for p in periods:
        needed.add(p.end)
        needed.update(p.prior_q_ends)
    earliest = min(needed)

    if args.instance_dir:
        facts = gather_facts_offline(args.instance_dir)
    else:
        if not args.user_agent:
            print('ERROR: aligned online mode needs --user-agent "Name email". '
                  "Offline? pass --companyfacts and --instance-dir.", file=sys.stderr)
            return 2
        filings = resolve_filings(args.cik, args.user_agent, earliest)
        if filings and filings[0][2] > earliest:
            print("WARNING: EDGAR's 'recent' submissions index may not reach back to "
                  f"{earliest}; the oldest quarter(s) could be missing. Reduce --periods "
                  "or extend resolve_filings() to page through filings['files'].",
                  file=sys.stderr)
        print(f"Fetching {len(filings)} filing instance(s) covering "
              f"{earliest} -> {periods[-1].end} ...")
        facts = gather_facts(args.cik, filings, args.user_agent)

    if facts.empty:
        print("No numeric facts parsed from any instance.", file=sys.stderr)
        return 1

    facts = explode_axes(facts)
    axes = resolve_axes(facts)
    coverage_report(facts, axes)

    wrote = 0
    # PRIMARY: revenue by market platform, quarterly, aligned to the panel.
    plat = aligned_quarterly_revenue(facts, periods, axes.get("product"), "Platform")
    if not plat.empty:
        print("\n--- Revenue by market platform (quarterly, aligned to panel) ---")
        print(plat.to_string())
        plat.to_csv(f"{args.outdir}/platform_revenue_quarterly_aligned.csv")
        wrote += 1
        print("  [note] ASC 606 disaggregation. Members were renamed at Q1 FY2027, so "
              "pre-/post-rename members appear in different columns by design; blanks are "
              "quarters in which a member did not exist. Fiscal Q4 columns are derived "
              "(FY less Q1-Q3), matching xbrl_panel.py.")
    else:
        print("\nNo product/platform axis found in the fetched instances "
              "(nothing to align for market platform).")

    # Companion panels on the same grid.
    seg = aligned_quarterly_revenue(facts, periods, axes.get("segment"), "Segment",
                                    cons_axis=axes.get("consolidation"), operating_only=True)
    if not seg.empty:
        print("\n--- Revenue by reportable segment (quarterly, aligned) ---")
        print(seg.to_string())
        seg.to_csv(f"{args.outdir}/segment_revenue_quarterly_aligned.csv")
        wrote += 1

    geo = aligned_quarterly_revenue(facts, periods, axes.get("geography"), "Geography")
    if not geo.empty:
        print("\n--- Revenue by geography (quarterly, aligned) ---")
        print(geo.to_string())
        geo.to_csv(f"{args.outdir}/geographic_revenue_quarterly_aligned.csv")
        wrote += 1

    facts.drop(columns=["dims"]).to_csv(f"{args.outdir}/dimensional_facts.csv", index=False)
    print(f"\nWrote {wrote} aligned panel(s) + dimensional_facts.csv to {args.outdir}/")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cik", type=int, default=1045810)
    ap.add_argument("--accession", default="", help="e.g. 000104581026000021")
    ap.add_argument("--instance", default="", help="local instance XML (offline; file must exist)")
    ap.add_argument("--latest", default="", metavar="FORM",
                    help="resolve the latest filing of this form, e.g. 10-Q or 10-K")
    ap.add_argument("--user-agent", default="")
    ap.add_argument("--period", default="both", choices=["annual", "quarter", "both"])
    ap.add_argument("--outdir", default="./out")
    # ---- aligned historical mode ----
    ap.add_argument("--periods", type=int, default=0,
                    help="ALIGNED MODE: build the last N fiscal quarters, matching "
                         "xbrl_panel.py --periods N, across multiple filing instances")
    ap.add_argument("--companyfacts", default="",
                    help="offline company-facts JSON, used only to build the aligned "
                         "period grid (skips the network for the grid)")
    ap.add_argument("--instance-dir", default="",
                    help="offline aligned mode: folder of extracted *_htm.xml instances")
    args = ap.parse_args(argv)
    os.makedirs(args.outdir, exist_ok=True)

    # Aligned historical mode takes precedence when --periods is given.
    if args.periods:
        return run_aligned(args)

    # -------- single-filing mode (unchanged) --------
    if args.instance:
        if not os.path.exists(args.instance):
            print(f"ERROR: no such file: {args.instance}\n"
                  f"  --instance is the OFFLINE path and needs the XML already on disk.\n"
                  f"  To download it instead, drop --instance and use:\n"
                  f'    python {os.path.basename(sys.argv[0])} --latest 10-Q '
                  f'--user-agent "Your Name you@firm.com"', file=sys.stderr)
            return 2
        content = open(args.instance, "rb").read()
    else:
        if not args.accession and not args.latest:
            print("ERROR: need --accession, --latest, --instance, or --periods", file=sys.stderr)
            return 2
        if not args.user_agent:
            print('ERROR: --user-agent is mandatory for EDGAR. Use a name and an email, '
                  'e.g. --user-agent "Jane Doe jane@firm.com"', file=sys.stderr)
            return 2
        accn = args.accession or latest_accession(args.cik, args.latest, args.user_agent)
        content = fetch_instance(args.cik, accn, args.user_agent)

    facts = parse_facts(content)
    if facts.empty:
        print("No numeric facts parsed — is this an instance document?", file=sys.stderr)
        return 1
    dim = facts[facts["n_dims"] > 0]
    print(f"Facts parsed: {len(facts):,}  ({len(dim):,} dimensioned, "
          f"{len(facts)-len(dim):,} default-context)")
    print(f"  -> the {len(facts)-len(dim):,} default-context facts are what Company "
          f"Facts would have returned; the {len(dim):,} dimensioned ones are why "
          f"this module exists.")

    facts = explode_axes(facts)
    axes = resolve_axes(facts)
    coverage_report(facts, axes)

    periods = ["annual", "quarter"] if args.period == "both" else [args.period]
    for per in periods:
        print(f"\n{'='*70}\n{per.upper()} PERIODS\n{'='*70}")

        seg, note = segment_analysis(facts, axes, per)
        for k, tbl in seg.items():
            if isinstance(tbl, pd.DataFrame) and not tbl.empty:
                print(f"\n--- Segment: {k} ---")
                print(tbl.to_string())
                tbl.to_csv(f"{args.outdir}/segment_{k}_{per}.csv")
        if note:
            print(f"  [note] {note}")

        geo, note = geographic_analysis(facts, axes, per)
        if not geo.empty:
            print(f"\n--- Geographic revenue ---")
            print(geo.to_string())
            geo.to_csv(f"{args.outdir}/geographic_{per}.csv")
            print(f"  [note] {note}")

        plat, note = platform_analysis(facts, axes, per)
        if not plat.empty:
            print(f"\n--- Revenue by platform ---")
            print(plat.to_string())
            plat.to_csv(f"{args.outdir}/platform_{per}.csv")
            print(f"  [note] {note}")

    facts.drop(columns=["dims"]).to_csv(f"{args.outdir}/dimensional_facts.csv", index=False)
    print(f"\nWrote outputs to {args.outdir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())