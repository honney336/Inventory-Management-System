from .locales import en, fi, ja, zh

DEFAULT_LOCALE = "en"

LOCALES = {
    "en": "English",
    "fi": "Suomi",
    "zh": "中文",
    "ja": "日本語",
}

CATALOGS = {
    "en": en.MESSAGES,
    "fi": fi.MESSAGES,
    "zh": zh.MESSAGES,
    "ja": ja.MESSAGES,
}