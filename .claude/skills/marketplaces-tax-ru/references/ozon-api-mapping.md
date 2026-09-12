# Ozon API → поля КУДиР/декларации

## Аутентификация

Заголовки на каждом запросе:
- `Client-Id: <OZON_CLIENT_ID>` (из .env)
- `Api-Key: <OZON_API_KEY>` (из .env)
- `Content-Type: application/json`

База: `https://api-seller.ozon.ru`. Все запросы POST.

## Эндпоинты, нужные для отчётности

### `POST /v2/finance/realization` — отчёт о реализации
**Назначение**: один отчёт за календарный месяц с агрегатом продаж.

Тело: `{"month": 1..12, "year": 2025}`.

Из ответа берём:
- `result.header.number` → **номер отчёта о реализации** (для графы 2 КУДиР)
- `result.header.doc_date` → дата отчёта (последний день месяца)
- `result.rows[]` → построчные продажи и возвраты

Расчёт суммы для КУДиР (cash-basis, метод B из methodology.md):
```python
sum(row['delivery_commission']['amount'] - row['return_commission']['amount']
    for row in rows)
```

⚠️ Это даёт buyer_paid net. Ozon Bank иногда показывает чуть другую сумму (на ~0.5-1% выше) из-за компенсаций и доставок. Для exact-match с Ozon Bank сверить с UI или захардкодить из эталона.

### `POST /v3/finance/transaction/list` — детальные транзакции
**Назначение**: построчный список всех операций (продажи, возвраты, услуги, компенсации, прочее).

Лимит: 1 месяц на запрос. Пагинация: `page`, `page_size` (max 1000).

Тело:
```json
{
  "filter": {
    "date": {"from": "2025-01-01T00:00:00.000Z", "to": "2025-01-31T23:59:59.999Z"},
    "transaction_type": "all"
  },
  "page": 1,
  "page_size": 1000
}
```

Ответ: `result.operations[]` с полями:
- `operation_id` — ID операции (НЕ номер документа компенсации)
- `operation_date`, `operation_type_name` — дата и описание
- `amount` — нетто-эффект на баланс продавца
- `accruals_for_sale` — доход от реализации (только для type=orders/returns)
- `delivery_charge`, `return_delivery_charge` — стоимость доставки
- `sale_commission` — комиссия Ozon (отрицательная)
- `type` — `orders` / `returns` / `services` / `compensation` / `other`

Для КУДиР интересны:
- `type='compensation'` → товарные компенсации (обычно единицы записей за год)

### `POST /v3/finance/transaction/totals` — агрегаты за период
**Назначение**: суммы по категориям за период (max 1 месяц).

Тело: `{"date":{"from":"...","to":"..."},"transaction_type":"all"}`.

Возвращаемые поля (рубли):
- `accruals_for_sale` — сумма реализации по seller_price (метод A)
- `sale_commission` — комиссия (отрицательная)
- `processing_and_delivery` — логистика (отрицательная)
- `refunds_and_cancellations` — возвраты денег
- `services_amount` — прочие услуги
- `compensation_amount` — компенсации
- `money_transfer` — банковские переводы
- `others_amount` — прочее

Сумма всех = чистый cash flow к продавцу за период.

### `POST /v1/finance/compensation` — отчёт о компенсациях
**Назначение**: получить **номера актов компенсаций**, которые не возвращаются в `/v3/finance/transaction/list`.

Async: запрос → `code` → `/v1/report/info` → ссылка на CSV/Excel.

Тело: `{"date": "2025-01"}`.

⚠️ Не реализовано в текущих скриптах. Когда будут компенсации в новом периоде — нужно доделать. Пока обходить вручную: ЛК Ozon → Финансы → Документы → Компенсации.

### `POST /v1/seller/info` — реквизиты магазина
Возвращает `result.company.name`, `legal_name`, `inn`, `tax_system` и рейтинги. Используется для проверки соединения.

## Что НЕ доступно через API

- **Проценты по накопительному счёту Ozon Bank** — только через банковскую выписку
- **Бухгалтерские проводки Ozon Bank** (его собственный расчёт УСН/НДС) — только через UI «Налоги» в Ozon Bank
- **Точная сумма «Доход» как у Ozon Bank** — приближается через `delivery_amount + compensation`, но расходится на ~0.5%
- **Номера актов компенсаций** в `/v3/finance/transaction/list` (нужен отдельный отчёт)

## Wildberries

Если магазин на WB пуст — игнорировать. Если появится активность:
- `GET /api/v5/supplier/reportDetailByPeriod` (statistics-api.wildberries.ru) — детали реализации
- Для УСН: суммировать `retail_price_withdisc_rub` по операциям типа «Продажа», вычитать «Возврат»
- Подробнее — в скилле `wildberries-api`

## Кеширование

Скрипты сохраняют сырой JSON в `data/raw/`:
- `ozon_realization_<year>-<MM>.json` — кеш `/v2/finance/realization`
- `ozon_transactions_<year>.json` — кеш `/v3/finance/transaction/list` (год целиком)

При повторном запуске данные не перетягиваются (для скорости). Если нужно обновить — удали кеш-файл.
