> **Safety warning (2026-11-14): Do not use this version to prepare or submit real payments.**
> A quoted CSV amount such as `12,50` can silently become `1250.00`; duplicate amount
> headers can also select the wrong value. Invalid dates and non-EUR currencies can pass.
> If you have used this tool, independently reconcile each amount, beneficiary, account,
> currency and date against the original instructions. Contact your bank promptly if a
> submitted instruction may be incorrect. A fix is under review; bank and schema
> validation have not been established. This notice does not reach existing clones.

# SEPA pain.001 CSV → ISO 20022 XML

Dependency-free Python CLI that converts a payment CSV into
**ISO 20022 `pain.001.001.03`** (SEPA Credit Transfer initiation).

Built for agent delivery (OpenTask SEPA one-off and similar jobs).

## Quick start

```bash
python3 sepa_pain001.py --self-test
python3 sepa_pain001.py --sample-csv > payments.csv
python3 sepa_pain001.py payments.csv -o pain001.xml
```

## CSV columns

| Column | Required | Notes |
|---|---|---|
| `debtor_name` | yes | Ordering party name |
| `debtor_iban` | yes | |
| `debtor_bic` | no | |
| `creditor_name` | yes | |
| `creditor_iban` | yes | |
| `creditor_bic` | no | |
| `amount` | yes | >0, max 2 dp |
| `currency` | no | default `EUR` |
| `remittance` | no | unstructured ≤140 |
| `end_to_end_id` | no | auto if empty |
| `execution_date` | no | `YYYY-MM-DD`, default today |

Aliases accepted: `from_iban`/`to_iban`, `payer_name`/`payee_name`, `amt`, `reference`, etc.

## Validation

- IBAN / BIC shape checks
- Positive amount, ≤2 decimal places
- Groups multiple creditors under one debtor into `PmtInf` batches
- Header `NbOfTxs` + `CtrlSum` match body

## License

MIT — Veriton (`veriton-dev`)
