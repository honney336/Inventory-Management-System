# Languages

| Code | Language |
|---|---|
| `en` | English (source language) |
| `fi` | Suomi |
| `zh` | 简体中文 |
| `ja` | 日本語 |

Each user picks their own language in the header, and the choice is remembered per account.
`DEFAULT_LOCALE` sets what a visitor sees before they choose.

Formatting follows the locale: Finnish writes `1 234,50` and `07.03.2026`, Chinese and
Japanese write `2026-03-07`, English writes `1,234.50` and `03/07/2026`.

## Catalogues

Catalogues live in `inventory/i18n/locales/`. The key is the English source string, so a
missing entry falls back to English instead of showing a raw key to a user.

Change a translation by editing the catalogue and restarting. There is nothing to compile.

```bash
flask --app app check-translations          # report missing entries
python scripts/extract_strings.py           # list every translatable string
python scripts/extract_strings.py --missing fi zh ja
```

`tests/test_inventory.py::test_every_locale_translates_every_string` fails if any translateable
string is missing from a locale, and a page-by-page render check runs every main page in all
four languages. A gap cannot survive a test run.

## Adding a language

1. Copy `inventory/i18n/locales/en.py` to `inventory/i18n/locales/<code>.py`.
2. Fill in every key. `scripts/extract_strings.py --missing <code>` lists what is left.
3. Add the code to `LOCALES` in `inventory/i18n/catalog.py`.
4. Add the code to `FORMATTERS` in `inventory/i18n/format.py` for number and date formatting.
5. Run `flask --app app check-translations` and the test suite.