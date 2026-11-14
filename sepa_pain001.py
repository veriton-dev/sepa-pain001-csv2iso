#!/usr/bin/env python3
"""CSV → ISO 20022 pain.001.001.03 (SEPA Credit Transfer) XML converter.

Minimal, dependency-free CLI for agent/OpenTask delivery.
Input CSV columns (header required, case-insensitive):
  debtor_name, debtor_iban, debtor_bic (optional),
  creditor_name, creditor_iban, creditor_bic (optional),
  amount, currency (default EUR), remittance (optional),
  end_to_end_id (optional), execution_date (optional YYYY-MM-DD)

Usage:
  python3 sepa_pain001.py payments.csv --reviewed-input -o pain001.xml
  python3 sepa_pain001.py --self-test
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
AMOUNT_RE = re.compile(r"^[0-9]+\.[0-9]{2}$")
# Local conservative bound: at most 18 digits total, including two cents
# digits. Bank and schema limits must be validated separately.
MAX_CENTS = 10**18 - 1


def _amount_cents(value: str) -> int:
    value = str(value).strip()
    if not AMOUNT_RE.fullmatch(value):
        raise ValueError("amount must use digits followed by a dot and exactly 2 decimals")
    whole, _, fraction = value.partition(".")
    # Strip insignificant zeroes before conversion, and limit length before int().
    whole = whole.lstrip("0") or "0"
    if len(whole) > 16:
        raise ValueError("amount exceeds 18 total digits with 2 decimals")
    cents = int(whole) * 100 + int(fraction.ljust(2, "0") or "0")
    if cents == 0:
        raise ValueError("amount must be >0")
    if cents > MAX_CENTS:
        raise ValueError("amount exceeds 18 total digits with 2 decimals")
    return cents


def _format_cents(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


BIC_RE = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def _clean_iban(v: str) -> str:
    return re.sub(r"\s+", "", (v or "").upper())


def _clean_bic(v: str) -> str:
    return re.sub(r"\s+", "", (v or "").upper())


def _msg_id() -> str:
    return "MSG" + uuid.uuid4().hex[:16].upper()


def _check_xml_text(value: str, label: str) -> None:
    # XML 1.0 legal characters, including exclusion of surrogate code points.
    if any(not (ord(c) in (9, 10, 13) or 0x20 <= ord(c) <= 0xD7FF
                or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)
           for c in value):
        raise ValueError(f"{label}: illegal XML character")


def _e2e(default_prefix: str, row_i: int, provided: str | None) -> str:
    if provided:
        if not re.fullmatch(r"[A-Za-z0-9/?\-+:().,' ]{1,35}", provided) or not provided.strip():
            raise ValueError(f"row {row_i}: invalid end_to_end_id (no sanitization performed)")
        return provided
    return f"{default_prefix}{row_i:04d}"[:35]


def validate_row(row: dict, idx: int) -> list[str]:
    errs: list[str] = []
    for key, value in row.items():
        if isinstance(value, str):
            try:
                _check_xml_text(value, f"row {idx} {key}")
            except ValueError as exc:
                errs.append(str(exc))
    for key, limit in (("debtor_name", 70), ("creditor_name", 70), ("remittance", 140)):
        if len(row.get(key) or "") > limit:
            errs.append(f"row {idx}: {key} exceeds {limit} characters")
    try:
        _e2e("E2E", idx, row.get("end_to_end_id"))
    except ValueError as exc:
        errs.append(str(exc))
    execution_date = (row.get("execution_date") or "").strip()
    if execution_date:
        try:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", execution_date):
                raise ValueError()
            date.fromisoformat(execution_date)
        except ValueError:
            errs.append(f"row {idx}: invalid execution_date")
    for req in (
        "debtor_name",
        "debtor_iban",
        "creditor_name",
        "creditor_iban",
        "amount",
    ):
        if not (row.get(req) or "").strip():
            errs.append(f"row {idx}: missing {req}")
    diban = _clean_iban(row.get("debtor_iban", ""))
    ciban = _clean_iban(row.get("creditor_iban", ""))
    if diban and not IBAN_RE.match(diban):
        errs.append(f"row {idx}: invalid debtor_iban")
    if ciban and not IBAN_RE.match(ciban):
        errs.append(f"row {idx}: invalid creditor_iban")
    for label, key in (("debtor_bic", "debtor_bic"), ("creditor_bic", "creditor_bic")):
        bic = _clean_bic(row.get(key, ""))
        if bic and not BIC_RE.match(bic):
            errs.append(f"row {idx}: invalid {label}")
    try:
        _amount_cents(row.get("amount", ""))
    except ValueError as exc:
        errs.append(f"row {idx}: {exc}")
    cur = (row.get("currency") or "EUR").strip().upper() or "EUR"
    if cur != "EUR":
        errs.append(f"row {idx}: currency must be EUR")
    return errs


def rows_from_csv(text: str) -> list[dict]:
    _check_xml_text(text, "CSV input")
    # strict=True rejects malformed quoted CSV rather than accepting partial records.
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        headers = next(reader, None)
    except csv.Error as exc:
        raise ValueError(f"invalid CSV header: {exc}") from exc
    if not headers:
        raise ValueError("CSV has no header row")
    mapping: dict[str, int] = {}
    for index, header in enumerate(headers):
        normalized = _norm_header(header)
        if not normalized:
            raise ValueError(f"empty CSV header at column {index + 1}")
        if normalized in mapping:
            raise ValueError(f"duplicate normalized CSV header: {normalized}")
        mapping[normalized] = index
    aliases = {
        "debtor_name": ["debtor_name", "dbtr_nm", "from_name", "payer_name"],
        "debtor_iban": ["debtor_iban", "dbtr_iban", "from_iban", "payer_iban"],
        "debtor_bic": ["debtor_bic", "dbtr_bic", "from_bic", "payer_bic"],
        "creditor_name": ["creditor_name", "cdtr_nm", "to_name", "payee_name"],
        "creditor_iban": ["creditor_iban", "cdtr_iban", "to_iban", "payee_iban"],
        "creditor_bic": ["creditor_bic", "cdtr_bic", "to_bic", "payee_bic"],
        "amount": ["amount", "amt", "value"],
        "currency": ["currency", "ccy", "curr"],
        "remittance": ["remittance", "rmtinf", "reference", "narration", "description"],
        "end_to_end_id": ["end_to_end_id", "e2e_id", "endtoendid", "payment_id"],
        "execution_date": ["execution_date", "reqd_exctn_dt", "date", "value_date"],
    }
    resolved: dict[str, int] = {}
    for canon, alts in aliases.items():
        found = [a for a in alts if a in mapping]
        if len(found) > 1:
            raise ValueError(f"conflicting CSV aliases for {canon}: {', '.join(found)}")
        if found:
            resolved[canon] = mapping[found[0]]
    out: list[dict] = []
    try:
        for raw in reader:
            # Empty physical lines are ignored by csv.reader; other records must
            # have exactly the header width, even if every field is blank.
            if not raw:
                continue
            if len(raw) != len(headers):
                raise ValueError(f"CSV record ending at line {reader.line_num}: expected {len(headers)} fields, got {len(raw)}")
            if not any(v.strip() for v in raw):
                continue
            out.append({k: (raw[src] if k == "end_to_end_id" else raw[src].strip()) for k, src in resolved.items()})
    except csv.Error as exc:
        raise ValueError(f"invalid CSV near line {reader.line_num}: {exc}") from exc
    return out


def build_pain001(
    rows: list[dict],
    *,
    initiating_party: str = "Veriton SEPA Converter",
    msg_id: str | None = None,
) -> str:
    if not rows:
        raise ValueError("no payment rows")
    for label, value, limit in (("initiating_party", initiating_party, 70), ("msg_id", msg_id, 35)):
        if value is not None:
            _check_xml_text(value, label)
            if not value.strip() or len(value) > limit:
                raise ValueError(f"{label} must be nonblank and at most {limit} characters")
    errs: list[str] = []
    for i, r in enumerate(rows, 1):
        errs.extend(validate_row(r, i))
    if errs:
        raise ValueError("; ".join(errs[:12]) + (f" (+{len(errs)-12} more)" if len(errs) > 12 else ""))

    mid = msg_id or _msg_id()
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    # Group by (debtor_iban, execution_date, currency) → one PmtInf each
    groups: dict[tuple[str, str, str], list[tuple[int, dict]]] = {}
    for i, r in enumerate(rows, 1):
        diban = _clean_iban(r["debtor_iban"])
        cur = (r.get("currency") or "EUR").strip().upper() or "EUR"
        ex = (r.get("execution_date") or "").strip() or date.today().isoformat()
        group = groups.setdefault((diban, ex, cur), [])
        if group:
            first = group[0][1]
            if (r["debtor_name"].strip() != first["debtor_name"].strip()
                    or _clean_bic(r.get("debtor_bic", "")) != _clean_bic(first.get("debtor_bic", ""))):
                raise ValueError(f"row {i}: conflicting debtor name or BIC in group")
        group.append((i, r))

    n_tx = len(rows)
    ctrl = sum(_amount_cents(r["amount"]) for r in rows)
    if ctrl > MAX_CENTS:
        raise ValueError("control sum exceeds 18 total digits with 2 decimals")

    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.03">',
        "  <CstmrCdtTrfInitn>",
        "    <GrpHdr>",
        f"      <MsgId>{escape(mid)}</MsgId>",
        f"      <CreDtTm>{created}</CreDtTm>",
        f"      <NbOfTxs>{n_tx}</NbOfTxs>",
        f"      <CtrlSum>{_format_cents(ctrl)}</CtrlSum>",
        "      <InitgPty>",
        f"        <Nm>{escape(initiating_party[:70])}</Nm>",
        "      </InitgPty>",
        "    </GrpHdr>",
    ]

    pmt_i = 0
    for (diban, ex, cur), items in groups.items():
        pmt_i += 1
        first = items[0][1]
        dname = first["debtor_name"]
        dbic = _clean_bic(first.get("debtor_bic", ""))
        g_sum = sum(_amount_cents(r["amount"]) for _, r in items)
        g_n = len(items)
        pmt_id = f"PMT{pmt_i:04d}"
        parts += [
            "    <PmtInf>",
            f"      <PmtInfId>{pmt_id}</PmtInfId>",
            "      <PmtMtd>TRF</PmtMtd>",
            "      <BtchBookg>false</BtchBookg>",
            f"      <NbOfTxs>{g_n}</NbOfTxs>",
            f"      <CtrlSum>{_format_cents(g_sum)}</CtrlSum>",
            "      <PmtTpInf>",
            "        <SvcLvl><Cd>SEPA</Cd></SvcLvl>",
            "      </PmtTpInf>",
            f"      <ReqdExctnDt>{escape(ex)}</ReqdExctnDt>",
            "      <Dbtr>",
            f"        <Nm>{escape(dname[:70])}</Nm>",
            "      </Dbtr>",
            "      <DbtrAcct>",
            "        <Id><IBAN>" + escape(diban) + "</IBAN></Id>",
            "      </DbtrAcct>",
        ]
        if dbic:
            parts += [
                "      <DbtrAgt>",
                "        <FinInstnId><BIC>" + escape(dbic) + "</BIC></FinInstnId>",
                "      </DbtrAgt>",
            ]
        else:
            parts += [
                "      <DbtrAgt>",
                "        <FinInstnId><Othr><Id>NOTPROVIDED</Id></Othr></FinInstnId>",
                "      </DbtrAgt>",
            ]

        for idx, r in items:
            amt = _amount_cents(r["amount"])
            ciban = _clean_iban(r["creditor_iban"])
            cbic = _clean_bic(r.get("creditor_bic", ""))
            cname = r["creditor_name"]
            e2e = _e2e("E2E", idx, r.get("end_to_end_id"))
            rmt = (r.get("remittance") or "").strip()
            parts += [
                "      <CdtTrfTxInf>",
                "        <PmtId>",
                f"          <EndToEndId>{escape(e2e)}</EndToEndId>",
                "        </PmtId>",
                f'        <Amt><InstdAmt Ccy="{escape(cur)}">{_format_cents(amt)}</InstdAmt></Amt>',
            ]
            if cbic:
                parts += [
                    "        <CdtrAgt>",
                    "          <FinInstnId><BIC>" + escape(cbic) + "</BIC></FinInstnId>",
                    "        </CdtrAgt>",
                ]
            parts += [
                "        <Cdtr>",
                f"          <Nm>{escape(cname[:70])}</Nm>",
                "        </Cdtr>",
                "        <CdtrAcct>",
                "          <Id><IBAN>" + escape(ciban) + "</IBAN></Id>",
                "        </CdtrAcct>",
            ]
            if rmt:
                parts += [
                    "        <RmtInf>",
                    f"          <Ustrd>{escape(rmt[:140])}</Ustrd>",
                    "        </RmtInf>",
                ]
            parts.append("      </CdtTrfTxInf>")
        parts.append("    </PmtInf>")

    parts += [
        "  </CstmrCdtTrfInitn>",
        "</Document>",
        "",
    ]
    return "\n".join(parts)


SAMPLE_CSV = """debtor_name,debtor_iban,debtor_bic,creditor_name,creditor_iban,creditor_bic,amount,currency,remittance,end_to_end_id,execution_date
ACME GmbH,DE89370400440532013000,COBADEFFXXX,Supplier SA,FR1420041010050500013M02606,PSSTFRPPXXX,12.50,EUR,Invoice 42,INV42-001,2026-09-15
ACME GmbH,DE89370400440532013000,COBADEFFXXX,Office BV,NL91ABNA0417164300,ABNANL2A,3.00,EUR,Office supplies,INV42-002,2026-09-15
"""


def self_test() -> int:
    rows = rows_from_csv(SAMPLE_CSV)
    xml = build_pain001(rows, initiating_party="Veriton Self-Test", msg_id="MSGSELFTEST0001")
    assert "pain.001.001.03" in xml
    assert "<NbOfTxs>2</NbOfTxs>" in xml
    assert "<CtrlSum>15.50</CtrlSum>" in xml
    assert "DE89370400440532013000" in xml
    assert "FR1420041010050500013M02606" in xml
    assert 'Ccy="EUR">12.50</InstdAmt>' in xml
    assert "Invoice 42" in xml
    # reject bad amount
    bad = rows_from_csv(
        "debtor_name,debtor_iban,creditor_name,creditor_iban,amount\n"
        "A,DE89370400440532013000,B,NL91ABNA0417164300,-1\n"
    )
    try:
        build_pain001(bad)
        raise AssertionError("expected validation error")
    except ValueError:
        pass
    print("self-test OK")
    print(xml)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="CSV → ISO 20022 pain.001.001.03 SEPA XML")
    p.add_argument("csv", nargs="?", help="input CSV path (or - for stdin)")
    p.add_argument("-o", "--output", help="output XML path (default stdout)")
    p.add_argument("--initiating-party", default="Veriton SEPA Converter")
    p.add_argument("--msg-id", default=None)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--reviewed-input", action="store_true", help="acknowledge independent review of this input for this run")
    p.add_argument("--sample-csv", action="store_true", help="print sample CSV and exit")
    args = p.parse_args(argv)

    if args.sample_csv:
        sys.stdout.write(SAMPLE_CSV)
        return 0
    if args.self_test:
        return self_test()
    if not args.csv:
        p.error("csv path required (or --self-test)")

    if not args.reviewed_input:
        p.error("--reviewed-input is required for every CSV conversion after independent input review")

    if args.csv == "-":
        text = sys.stdin.read()
    else:
        text = Path(args.csv).read_text(encoding="utf-8")

    rows = rows_from_csv(text)
    xml = build_pain001(
        rows,
        initiating_party=args.initiating_party,
        msg_id=args.msg_id,
    )
    if args.output:
        Path(args.output).write_text(xml, encoding="utf-8")
        print(f"wrote {args.output} txs={len(rows)}", file=sys.stderr)
    else:
        sys.stdout.write(xml)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
