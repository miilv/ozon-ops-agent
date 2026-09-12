"""Generate УСН XML declaration for ИП on УСН Доходы.

Usage:
  python3 build_usn_xml.py YEAR DOC_DATE Q1_INCOME H1_INCOME 9M_INCOME Y_INCOME [НомКорр]

Example (первичная):
  python3 build_usn_xml.py 2025 01.05.2026 3000000 6000000 9000000 12000000
Example (корректировка №1):
  python3 build_usn_xml.py 2025 09.07.2026 3000000 6000000 9000000 12500000 1

Источник доходов — Ozon Bank → Налоги → УСН.

Скрипт читает реквизиты из .env корня проекта (USER_INN, USER_OKTMO,
USER_TAX_RATE_LAW_CODE, и т.д.) — см. SKILL.md.

ВАЖНО: генератор пишет раздел 1.1 (АвПУКв/АвПУУменПг/АвПУУмен9м/НалПУУменПер).
Пропуск этих атрибутов = раздел 1.1 читается ФНС как нули → требование о пояснениях
(типовая причина требования от ФНС).

Reads .env from cwd or its parents (max 3 levels up).
"""
import os, sys, uuid, pathlib

# Find .env
def find_env() -> pathlib.Path:
    cwd = pathlib.Path.cwd()
    for d in [cwd] + list(cwd.parents)[:3]:
        f = d / ".env"
        if f.exists():
            return f
    raise FileNotFoundError(".env not found in cwd or up to 3 parents")

ENV = find_env()
for ln in ENV.read_text().splitlines():
    if ln.startswith("#") or "=" not in ln:
        continue
    k, v = ln.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip())

if len(sys.argv) < 7:
    print(__doc__)
    sys.exit(1)

year = int(sys.argv[1])
doc_date = sys.argv[2]            # DD.MM.YYYY
q1, h1, m9, yr = (int(x) for x in sys.argv[3:7])
korr = int(sys.argv[7]) if len(sys.argv) > 7 else 0   # НомКорр: 0 первичная, 1+ корректировка

inn = os.environ["USER_INN"]
oktmo = os.environ["USER_OKTMO"]
fns = os.environ["USER_FNS_CODE"]   # код своей инспекции; дефолта быть не должно
law = os.environ["USER_TAX_RATE_LAW_CODE"]
rate = int(os.environ["USER_TAX_RATE_PERCENT"])
last = os.environ["USER_LAST_NAME"]
first = os.environ["USER_FIRST_NAME"]
middle = os.environ["USER_MIDDLE_NAME"]

# Compute Исчисл (rate * income, rounded to integer rubles)
i1 = round(q1 * rate / 100)
i2 = round(h1 * rate / 100)
i3 = round(m9 * rate / 100)
i4 = round(yr * rate / 100)

# Compute УменНал — capped at Исчисл and at total contribution
fixed = 53_658                            # 2025 fixed contributions; update for other years
cap_1pct = 300_888                        # 2025 cap on 1% income contributions
contrib_1pct_q1 = max(0, (q1 - 300_000) * 0.01)
total_contrib = fixed + min(cap_1pct, max(0, (yr - 300_000) * 0.01))
contrib_h1 = fixed + min(cap_1pct, max(0, (h1 - 300_000) * 0.01))

u1 = min(i1, round(fixed + contrib_1pct_q1))
u2 = min(i2, round(contrib_h1))
u3 = min(i3, round(total_contrib))
u4 = min(i4, round(total_contrib))

# Раздел 1.1 — суммы к уплате нарастающим, затем разности (строки 020/040/070/100).
# Каждая строка = прирост обязанности за период относительно предыдущего.
c1 = max(0, i1 - u1)              # накопит. к уплате за Q1
c2 = max(0, i2 - u2)              # за полугодие
c3 = max(0, i3 - u3)              # за 9 мес
c4 = max(0, i4 - u4)             # за год
r020 = c1                         # АвПУКв
r040 = c2 - c1                    # АвПУУменПг  (может быть <0 — к уменьшению)
r070 = c3 - c2                    # АвПУУмен9м
r100 = c4 - c3                    # НалПУУменПер

guid = str(uuid.uuid4()).upper()
date_compact = doc_date.replace(".","").zfill(8)
date_compact = date_compact[4:] + date_compact[2:4] + date_compact[:2]  # DDMMYYYY → YYYYMMDD
fid = f"NO_USN_{fns}_{fns}_{inn}_{date_compact}_{guid}"

xml = (
    '<?xml version="1.0" encoding="windows-1251"?>\n'
    f'<Файл xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    f'ИдФайл="{fid}" ВерсПрог="Налогоплательщик ЮЛ 4.90" ВерсФорм="5.08">'
    f'<Документ КНД="1152017" ДатаДок="{doc_date}" Период="34" ОтчетГод="{year}" '
    f'КодНО="{fns}" НомКорр="{korr}" ПоМесту="120">'
    f'<СвНП><НПФЛ ИННФЛ="{inn}">'
    f'<ФИО Фамилия="{last}" Имя="{first}" Отчество="{middle}"/>'
    '</НПФЛ></СвНП>'
    '<Подписант ПрПодп="1"/>'
    '<УСН ОбНал="1">'
    f'<СумНалПУ_НП ОКТМО="{oktmo}" АвПУКв="{r020}" АвПУУменПг="{r040}" '
    f'АвПУУмен9м="{r070}" НалПУУменПер="{r100}">'
    '<РасчНал1 ПризСтав="1" ПризНП="2">'
    f'<Доход СумЗаКв="{q1}" СумЗаПг="{h1}" СумЗа9м="{m9}" СумЗаНалПер="{yr}"/>'
    f'<Ставка СтавкаКв="{rate}" СтавкаПг="{rate}" Ставка9м="{rate}" СтавкаНалПер="{rate}" КодЛьгот="{law}"/>'
    f'<Исчисл СумЗаКв="{i1}" СумЗаПг="{i2}" СумЗа9м="{i3}" СумЗаНалПер="{i4}"/>'
    f'<УменНал СумЗаКв="{u1}" СумЗаПг="{u2}" СумЗа9м="{u3}" СумЗаНалПер="{u4}"/>'
    '</РасчНал1></СумНалПУ_НП></УСН></Документ></Файл>\n'
)

out_dir = ENV.parent / "data" / "nalog"
out_dir.mkdir(parents=True, exist_ok=True)
out = out_dir / f"{fid}.xml"
out.write_bytes(xml.encode("windows-1251"))
print(f"Saved: {out}")
print(f"НомКорр: {korr}")
print(f"Раздел 1.1: 020={r020}  040={r040}  070={r070}  100={r100}")
print(f"Годовая обязанность (Σ разделов 1.1): {r020 + r040 + r070 + r100:,} ₽".replace(",", " "))
print(f"Налог к доплате за год (строка 100): {r100:,} ₽".replace(",", " "))
