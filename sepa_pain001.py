#!/usr/bin/env python3
"""CSV → ISO 20022 pain.001.001.03 (SEPA Credit Transfer) XML converter.

Minimal, dependency-free CLI for agent/OpenTask delivery.
Input CSV columns (header required, case-insensitive):
  debtor_name, debtor_iban, debtor_bic (optional),
  creditor_name, creditor_iban, creditor_bic (optional),
  amount, currency (default EUR), remittance (optional),
  end_to_end_id (optional), execution_date (optional YYYY-MM-DD)

Usage:
  python3 sepa_pain001.py payments.csv -o pain001.xml
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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.sax.saxutils import escape

IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
BIC_RE = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def _clean_iban(v: str) -> str:
    return re.sub(r"\s+", "", (v or "").upper())


def _clean_bic(v: str) -> str:
    return re.sub(r"\s+", "", (v or "").upper())


def _msg_id() -> str:
    return "MSG" + uuid.uuid4().hex[:16].upper()


def _e2e(default_prefix: str, row_i: int, provided: str | None) -> str:
    if provided and provided.strip():
        return re.sub(r"[^A-Za-z0-9/?\-+:().,' ]", "", provided.strip())[:35]
    return f"{default_prefix}{row_i:04d}"[:35]


def validate_row(row: dict, idx: int) -> list[str]:
    errs: list[str] = []
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
        amt = Decimal(str(row.get("amount", "")).strip().replace(",", ""))
        if amt <= 0 or amt.as_tuple().exponent < -2:
            errs.append(f"row {idx}: amount must be >0 with max 2 decimal places")
    except (InvalidOperation, ValueError):
        errs.append(f"row {idx}: amount not a number")
    cur = (row.get("currency") or "EUR").strip().upper() or "EUR"
    if len(cur) != 3 or not cur.isalpha():
        errs.append(f"row {idx}: currency must be ISO-4217 alpha-3")
    return errs


def rows_from_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    mapping = {_norm_header(h): h for h in reader.fieldnames if h}
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
    resolved: dict[str, str] = {}
    for canon, alts in aliases.items():
        for a in alts:
            if a in mapping:
                resolved[canon] = mapping[a]
                break
    out: list[dict] = []
    for raw in reader:
        if not any((v or "").strip() for v in raw.values()):
            continue
        row = {k: (raw.get(src) or "").strip() for k, src in resolved.items()}
        out.append(row)
    return out


def build_pain001(
    rows: list[dict],
    *,
    initiating_party: str = "Veriton SEPA Converter",
    msg_id: str | None = None,
) -> str:
    if not rows:
        raise ValueError("no payment rows")
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
        groups.setdefault((diban, ex, cur), []).append((i, r))

    n_tx = len(rows)
    ctrl = sum(Decimal(str(r["amount"]).replace(",", "")) for r in rows)

    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.03">',
        "  <CstmrCdtTrfInitn>",
        "    <GrpHdr>",
        f"      <MsgId>{escape(mid)}</MsgId>",
        f"      <CreDtTm>{created}</CreDtTm>",
        f"      <NbOfTxs>{n_tx}</NbOfTxs>",
        f"      <CtrlSum>{ctrl:.2f}</CtrlSum>",
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
        g_sum = sum(Decimal(str(r["amount"]).replace(",", "")) for _, r in items)
        g_n = len(items)
        pmt_id = f"PMT{pmt_i:04d}"
        parts += [
            "    <PmtInf>",
            f"      <PmtInfId>{pmt_id}</PmtInfId>",
            "      <PmtMtd>TRF</PmtMtd>",
            "      <BtchBookg>false</BtchBookg>",
            f"      <NbOfTxs>{g_n}</NbOfTxs>",
            f"      <CtrlSum>{g_sum:.2f}</CtrlSum>",
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
            amt = Decimal(str(r["amount"]).replace(",", ""))
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
                f'        <Amt><InstdAmt Ccy="{escape(cur)}">{amt:.2f}</InstdAmt></Amt>',
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
    p.add_argument("--sample-csv", action="store_true", help="print sample CSV and exit")
    args = p.parse_args(argv)

    if args.sample_csv:
        sys.stdout.write(SAMPLE_CSV)
        return 0
    if args.self_test:
        return self_test()
    if not args.csv:
        p.error("csv path required (or --self-test)")

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
