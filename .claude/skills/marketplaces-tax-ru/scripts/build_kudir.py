"""
Generate КУДиР (Книга учёта доходов и расходов) for ИП on УСН Доходы.

Структура соответствует эталону (приказ ФНС от 07.11.2023 № ЕА-7-3/816@, Приложение № 2).
Источники данных:
  - Реквизиты ИП        — из .env
  - Отчёты о реализации — Ozon API /v2/finance/realization (header.number)
  - Товарные компенсации— Ozon API /v3/finance/transaction/list (type='compensation')
                          ВНИМАНИЕ: API даёт operation_id, а не "номер документа компенсации".
                          Реальные номера актов тянутся через
                          /v1/finance/compensation (async-отчёт), либо вручную из
                          ЛК Ozon → Финансы → Документы → Компенсации.
  - Проценты по накоп.счёту — выписка Ozon Bank, добавляются вручную через `extra_entries`

Запуск: python3 scripts/build_kudir.py 2025
"""

import json, os, pathlib, sys, calendar, urllib.request, copy, shutil
from datetime import date
from openpyxl import load_workbook
from openpyxl.styles import Font, Alignment, Border, Side
from copy import copy as styled_copy

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
RAW = ROOT / "data" / "raw"
NALOG = ROOT / "data" / "nalog"
TEMPLATE = NALOG / "templates" / "КУДиР_шаблон.xlsx"
REFERENCE = NALOG / "эталон_КУДиР_2025.xlsx"

# ----- env -----
for ln in ENV.read_text().splitlines():
    if ln.startswith("#") or "=" not in ln:
        continue
    k, v = ln.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip())

CID = os.environ["OZON_CLIENT_ID"]
KEY = os.environ["OZON_API_KEY"]


def http(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"https://api-seller.ozon.ru{path}",
        data=json.dumps(body).encode(),
        headers={"Client-Id": CID, "Api-Key": KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


# ----- API: отчёты о реализации -----
def realization_report(year: int, month: int) -> dict:
    """Returns header dict: {number, doc_date, start_date, stop_date, ...}"""
    cache = RAW / f"ozon_realization_{year}-{month:02d}.json"
    if cache.exists():
        data = json.load(open(cache))
    else:
        data = http("/v2/finance/realization", {"month": month, "year": year})
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return data["result"]["header"]


# ----- API: компенсации -----
def compensations_year(year: int) -> list:
    """Returns list of {date, amount, op_type_name, operation_id}.
    Note: operation_id ≠ официальный номер акта компенсации.
    Для номера акта используй ЛК Ozon → Финансы → Документы → Компенсации."""
    cache = RAW / f"ozon_transactions_{year}.json"
    if cache.exists():
        ops = json.load(open(cache))
    else:
        ops = []
        for m in range(1, 13):
            last = calendar.monthrange(year, m)[1]
            page = 1
            while True:
                d = http("/v3/finance/transaction/list", {
                    "filter": {"date":{"from":f"{year}-{m:02d}-01T00:00:00.000Z","to":f"{year}-{m:02d}-{last:02d}T23:59:59.999Z"},"transaction_type":"all"},
                    "page": page, "page_size": 1000
                })
                opss = d["result"].get("operations", [])
                if not opss:
                    break
                ops.extend(opss)
                if page >= d["result"].get("page_count", 0):
                    break
                page += 1
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(ops, ensure_ascii=False))

    return [
        {
            "date": o["operation_date"][:10],
            "amount": o.get("amount", 0),
            "op_type_name": o.get("operation_type_name", ""),
            "operation_id": o.get("operation_id"),
        }
        for o in ops
        if o.get("type") == "compensation"
    ]


# ----- Сбор записей для Раздела I -----
def build_entries(year: int, comp_doc_numbers: dict | None = None,
                  extra_entries: list | None = None) -> list:
    """Returns list of dicts with keys: date, doc_no, content, amount, kind.
    kind: 'sale' | 'compensation' | 'other'.

    comp_doc_numbers: { 'YYYY-MM-DD': '<номер акта>' } — официальные номера актов.
    extra_entries: дополнительные записи (проценты по счёту и т.п.).
    """
    contract = f"{os.environ['OZON_AGENCY_CONTRACT_NUMBER']} от {os.environ['OZON_AGENCY_CONTRACT_DATE']}"
    entries = []

    # Sales
    for m in range(1, 13):
        h = realization_report(year, m)
        d = h["doc_date"]  # YYYY-MM-DD
        no = h["number"]
        # Сумма реализации = sum amount across rows in this report. Для согласования с Ozon Bank
        # КУДиР использует НЕТТО buyer_paid. Здесь берём из per-row data:
        rows = json.load(open(RAW / f"ozon_realization_{year}-{m:02d}.json"))["result"]["rows"]
        def safe(x, *p):
            for k in p:
                if not isinstance(x, dict): return 0
                x = x.get(k)
            return x or 0
        amount = sum(safe(r,'delivery_commission','amount') - safe(r,'return_commission','amount') for r in rows)
        entries.append({
            "date": d,
            "doc_no": f"№{no}",
            "content": f"Оплата от покупателей OZON по Отчету о реализации №{no} от {d[8:10]}.{d[5:7]}.{d[:4]}. Договор № {contract}",
            "amount": round(amount, 2),
            "kind": "sale",
        })

    # Compensations from API
    comps = compensations_year(year)
    comp_doc_numbers = comp_doc_numbers or {}
    for c in comps:
        # Дата компенсации: API даёт operation_date, но в эталоне дата компенсации совпадает с датой ближайшего отчёта.
        # Берём конец месяца этой даты как fallback (для согласованности с эталоном).
        d = c["date"]
        y, m, _ = d.split("-")
        last = calendar.monthrange(int(y), int(m))[1]
        d_eom = f"{y}-{m}-{last:02d}"
        official_no = comp_doc_numbers.get(d) or comp_doc_numbers.get(d_eom) or f"op_id={c['operation_id']}"
        entries.append({
            "date": d_eom,
            "doc_no": f"№{official_no}",
            "content": f"Товарная компенсация OZON №{official_no} от {d_eom[8:10]}.{d_eom[5:7]}.{d_eom[:4]}",
            "amount": c["amount"],
            "kind": "compensation",
        })

    # Extras
    for e in (extra_entries or []):
        entries.append(e)

    # Sort chronologically; sales after compensation on same day (как в эталоне)
    kind_order = {"compensation": 0, "other": 1, "sale": 2}
    entries.sort(key=lambda x: (x["date"], kind_order.get(x["kind"], 9)))
    return entries


# ----- Заполнение Excel -----
def render(year: int, entries: list, out_path: pathlib.Path):
    """Render КУДиР from entries into Excel by cloning the reference and replacing values."""
    shutil.copy(REFERENCE, out_path)
    wb = load_workbook(out_path)

    # Title page
    t = wb["Титульный лист"]
    t["L18"] = os.environ["USER_FULL_NAME"]
    t["K16"] = f"на {year} год"
    t["Y21"] = os.environ["USER_OKPO"]
    inn = os.environ["USER_INN"]
    cols = "BCDEFGHIJKLM"  # 12 cells for ИНН of ИП
    for i, col in enumerate(cols):
        t[f"{col}30"] = inn[i] if i < len(inn) else ""
    t["I33"] = os.environ["USER_TAX_OBJECT"]
    accs = [os.environ.get("USER_OZON_BANK_ACCOUNT_1"), os.environ.get("USER_OZON_BANK_ACCOUNT_2")]
    bank = os.environ["USER_OZON_BANK_NAME"]
    accs_text = ", ".join(f'№ {a} в {bank}' for a in accs if a)
    t["B43"] = accs_text

    # Group entries into quarters
    quarters = [[], [], [], []]
    for e in entries:
        m = int(e["date"][5:7])
        quarters[(m - 1) // 3].append(e)

    cum = 0
    seq = 0
    quarter_sheets = [
        ("Раздел I. Доходы и расходы за 1", "I квартал", []),
        ("Раздел I. Доходы и расходы за 2", "II квартал", ["полугодие"]),
        ("Раздел I. Доходы и расходы за 3", "III квартал", ["9 месяцев"]),
        ("Раздел I. Доходы и расходы за 4", "IV квартал", ["год"]),
    ]
    cum_label_to_value = {}

    for q_idx, (sheet_name, q_label, extra_totals) in enumerate(quarter_sheets):
        ws = wb[sheet_name]
        # Удалить ВСЕ merged ranges кроме шапочных, ДО очистки данных
        keep = {"B2:D2", "B4:D4", "E4:F4"}
        to_remove = [str(r) for r in ws.merged_cells.ranges if str(r) not in keep]
        for r in to_remove:
            ws.unmerge_cells(r)
        # Теперь очистить data-строки
        max_existing = ws.max_row
        for r in range(7, max_existing + 1):
            for c in range(1, ws.max_column + 1):
                ws.cell(r, c).value = None

        row = 7
        q_sum = 0
        for e in quarters[q_idx]:
            seq += 1
            d = e["date"]
            d_str = f"{d[8:10]}.{d[5:7]}.{d[:4]}"
            ws.cell(row, 2).value = seq
            # "Б/Н" пишется без префикса "№", остальные с "№ "
            doc_field = e['doc_no'] if e['doc_no'].startswith('Б/Н') else f"№ {e['doc_no']}"
            ws.cell(row, 3).value = f"{d_str}, {doc_field}"
            ws.cell(row, 4).value = e["content"]
            ws.cell(row, 5).value = e["amount"]
            for c in range(2, 7):
                ws.cell(row, c).font = Font(name="Times New Roman", size=10)
                ws.cell(row, c).border = Border(
                    left=Side(border_style="thin"), right=Side(border_style="thin"),
                    top=Side(border_style="thin"), bottom=Side(border_style="thin"))
                ws.cell(row, c).alignment = Alignment(wrap_text=True, vertical="top",
                    horizontal=("center" if c in (2,3) else "right" if c in (5,6) else "left"))
            ws.cell(row, 5).number_format = '#,##0.00'
            q_sum += e["amount"]
            row += 1

        # Итого за квартал
        ws.cell(row, 2).value = f"Итого за {q_label}"
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
        ws.cell(row, 5).value = round(q_sum, 2)
        ws.cell(row, 5).number_format = '#,##0.00'
        for c in range(2, 7):
            ws.cell(row, c).font = Font(name="Times New Roman", size=10, bold=True)
            ws.cell(row, c).border = Border(
                left=Side(border_style="thin"), right=Side(border_style="thin"),
                top=Side(border_style="thin"), bottom=Side(border_style="thin"))
            ws.cell(row, c).alignment = Alignment(horizontal=("right" if c==5 else "left"), vertical="center")
        cum += q_sum
        row += 1

        for label in extra_totals:
            ws.cell(row, 2).value = f"Итого за {label}"
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
            ws.cell(row, 5).value = round(cum, 2)
            ws.cell(row, 5).number_format = '#,##0.00'
            for c in range(2, 7):
                ws.cell(row, c).font = Font(name="Times New Roman", size=10, bold=True)
                ws.cell(row, c).border = Border(
                    left=Side(border_style="thin"), right=Side(border_style="thin"),
                    top=Side(border_style="thin"), bottom=Side(border_style="thin"))
                ws.cell(row, c).alignment = Alignment(horizontal=("right" if c==5 else "left"), vertical="center")
            cum_label_to_value[label] = round(cum, 2)
            row += 1

        # Справка к разделу I — только на последнем (Q4) листе. Структура 1:1 с эталоном.
        if q_idx == 3:
            row += 1  # blank row before справка
            spacer = ' ' * 62
            ws.cell(row, 2).value = f"Справка к разделу I:{spacer}"
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
            row += 2  # skip a row
            # 010
            ws.cell(row, 2).value = "010 "
            ws.cell(row, 3).value = "Сумма полученных доходов за налоговый период"
            ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=5)
            ws.cell(row, 6).value = round(cum, 2)
            ws.cell(row, 6).number_format = '#,##0.00'
            row += 1
            # 020
            ws.cell(row, 2).value = "020"
            ws.cell(row, 3).value = "Сумма произведенных  расходов за налоговый период"
            ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=5)
            row += 1
            # 030 (split into two rows like reference)
            ws.cell(row, 2).value = "030"
            ws.cell(row, 3).value = "Сумма разницы между  суммой уплаченного минимального налога и суммой"
            ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=5)
            row += 1
            ws.cell(row, 3).value = "исчисленного в общем порядке налога за предыдущий налоговый период"
            ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=5)
            row += 1
            # Итого получено:
            ws.cell(row, 2).value = "Итого получено:" + ' ' * 47
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
            row += 1
            # 040
            ws.cell(row, 2).value = "040" + ' ' * 49
            ws.cell(row, 3).value = "- доходов    "
            ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)
            row += 1
            ws.cell(row, 3).value = "(код стр. 010 - код  стр. 020 - код стр. 030)"
            ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)
            ws.cell(row, 6).value = round(cum, 2)
            ws.cell(row, 6).number_format = '#,##0.00'
            row += 1
            # 041
            ws.cell(row, 2).value = "041" + ' ' * 52
            ws.cell(row, 3).value = "- убытков"
            ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)
            row += 1
            ws.cell(row, 3).value = "(код стр. 020 + код  стр. 030) - код стр. 010)"
            ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)

    wb.save(out_path)
    return cum


def canonical_entries_example() -> list:
    """Пример «ручного» набора записей за год.

    Когда он нужен: реестр собран не из API, а сверен с эталоном (выписка,
    прошлогодняя КУДиР, данные бухгалтера) и порядок строк нужно зафиксировать
    явно. Замените строки ниже своими; суммы здесь синтетические.

    Обычный путь — `build_entries(year)`: собирает записи из отчётов о
    реализации через API (см. pull_ozon_realization.py).
    """
    contract = f"№ {os.environ['OZON_AGENCY_CONTRACT_NUMBER']} от {os.environ['OZON_AGENCY_CONTRACT_DATE']}"
    # ordered list of (date, doc_no, amount, kind)
    rows = [
        ("2025-01-31", "1000001", 1_000_000.00, "sale"),
        ("2025-02-28", "1000002",    50_000.00, "compensation"),
        ("2025-02-28", "1000003", 1_200_000.00, "sale"),
        ("2025-03-31", "Б/Н",            43.84, "interest"),
    ]
    out = []
    for d, no, amt, kind in rows:
        ddmm = f"{d[8:10]}.{d[5:7]}.{d[:4]}"
        if kind == "sale":
            doc = f"№{no}"
            content = f"Оплата от покупателей OZON по Отчету о реализации №{no} от {ddmm}. Договор {contract}"
        elif kind == "compensation":
            doc = f"№{no}"
            content = f"Товарная компенсация OZON №{no} от {ddmm}"
        elif kind == "interest":
            doc = no  # Б/Н — без знака №
            content = f"Выплата процентов по накопительному счету №{os.environ['USER_OZON_BANK_ACCOUNT_2']} за {ddmm}. НДС не облагается"
        out.append({"date": d, "doc_no": doc, "amount": amt, "kind": kind, "content": content})
    return out


if __name__ == "__main__":
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    entries = build_entries(year)

    out = NALOG / f"КУДиР_{year}_ИП.xlsx"
    total = render(year, entries, out)
    print(f"Saved: {out}")
    print(f"Total entries: {len(entries)}")
    print(f"Year sum: {total:,.2f} ₽")
