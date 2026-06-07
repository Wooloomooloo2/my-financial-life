"""ISO 4217 currency codes — the canonical list of active currencies.

Hardcoded rather than pulled from a library because the list is finite,
changes rarely (one to two updates per year, almost always additions),
and adding a runtime dependency for ~180 short strings isn't worth it.
Update the list by adding/removing rows when ISO publishes a revision;
the next revision after the most recent issue date is what to chase.

Used by the Account dialog (typeahead combo + validation) so a typo
like "GPB" or "USX" never lands in `account.currency` and silently
breaks FX lookups downstream.

Each entry is `(code, name)`; `code` is the 3-letter ISO 4217 alpha
code, `name` is the English short name (for the dropdown — users pick
by code but seeing the name disambiguates similar codes like SEK/SGD).
The list is sorted by code so the dropdown reads in stable order;
the Account dialog's editable-combo typeahead handles search.

Sources: ISO 4217:2015 as amended through the 2024 list maintained by
the ISO 4217 Maintenance Agency (SIX Group). Active fund codes (XAU,
XAG etc.) and the supranational specials (XDR, XSU, XUA) are excluded
— personal-finance accounts almost never hold them, and including
them clutters the dropdown.
"""
from __future__ import annotations

# Sorted by code. Keep this in alpha order so reviews of additions are
# obvious as inserts at the right spot.
ISO_4217_CURRENCIES: tuple[tuple[str, str], ...] = (
    ("AED", "UAE Dirham"),
    ("AFN", "Afghani"),
    ("ALL", "Lek"),
    ("AMD", "Armenian Dram"),
    ("ANG", "Netherlands Antillean Guilder"),
    ("AOA", "Kwanza"),
    ("ARS", "Argentine Peso"),
    ("AUD", "Australian Dollar"),
    ("AWG", "Aruban Florin"),
    ("AZN", "Azerbaijan Manat"),
    ("BAM", "Convertible Mark"),
    ("BBD", "Barbados Dollar"),
    ("BDT", "Taka"),
    ("BGN", "Bulgarian Lev"),
    ("BHD", "Bahraini Dinar"),
    ("BIF", "Burundi Franc"),
    ("BMD", "Bermudian Dollar"),
    ("BND", "Brunei Dollar"),
    ("BOB", "Boliviano"),
    ("BRL", "Brazilian Real"),
    ("BSD", "Bahamian Dollar"),
    ("BTN", "Ngultrum"),
    ("BWP", "Pula"),
    ("BYN", "Belarusian Ruble"),
    ("BZD", "Belize Dollar"),
    ("CAD", "Canadian Dollar"),
    ("CDF", "Congolese Franc"),
    ("CHF", "Swiss Franc"),
    ("CLP", "Chilean Peso"),
    ("CNY", "Yuan Renminbi"),
    ("COP", "Colombian Peso"),
    ("CRC", "Costa Rican Colon"),
    ("CUP", "Cuban Peso"),
    ("CVE", "Cabo Verde Escudo"),
    ("CZK", "Czech Koruna"),
    ("DJF", "Djibouti Franc"),
    ("DKK", "Danish Krone"),
    ("DOP", "Dominican Peso"),
    ("DZD", "Algerian Dinar"),
    ("EGP", "Egyptian Pound"),
    ("ERN", "Nakfa"),
    ("ETB", "Ethiopian Birr"),
    ("EUR", "Euro"),
    ("FJD", "Fiji Dollar"),
    ("FKP", "Falkland Islands Pound"),
    ("GBP", "Pound Sterling"),
    ("GEL", "Lari"),
    ("GHS", "Ghana Cedi"),
    ("GIP", "Gibraltar Pound"),
    ("GMD", "Dalasi"),
    ("GNF", "Guinean Franc"),
    ("GTQ", "Quetzal"),
    ("GYD", "Guyana Dollar"),
    ("HKD", "Hong Kong Dollar"),
    ("HNL", "Lempira"),
    ("HTG", "Gourde"),
    ("HUF", "Forint"),
    ("IDR", "Rupiah"),
    ("ILS", "New Israeli Sheqel"),
    ("INR", "Indian Rupee"),
    ("IQD", "Iraqi Dinar"),
    ("IRR", "Iranian Rial"),
    ("ISK", "Iceland Krona"),
    ("JMD", "Jamaican Dollar"),
    ("JOD", "Jordanian Dinar"),
    ("JPY", "Yen"),
    ("KES", "Kenyan Shilling"),
    ("KGS", "Som"),
    ("KHR", "Riel"),
    ("KMF", "Comorian Franc"),
    ("KPW", "North Korean Won"),
    ("KRW", "Won"),
    ("KWD", "Kuwaiti Dinar"),
    ("KYD", "Cayman Islands Dollar"),
    ("KZT", "Tenge"),
    ("LAK", "Lao Kip"),
    ("LBP", "Lebanese Pound"),
    ("LKR", "Sri Lanka Rupee"),
    ("LRD", "Liberian Dollar"),
    ("LSL", "Loti"),
    ("LYD", "Libyan Dinar"),
    ("MAD", "Moroccan Dirham"),
    ("MDL", "Moldovan Leu"),
    ("MGA", "Malagasy Ariary"),
    ("MKD", "Denar"),
    ("MMK", "Kyat"),
    ("MNT", "Tugrik"),
    ("MOP", "Pataca"),
    ("MRU", "Ouguiya"),
    ("MUR", "Mauritius Rupee"),
    ("MVR", "Rufiyaa"),
    ("MWK", "Malawi Kwacha"),
    ("MXN", "Mexican Peso"),
    ("MYR", "Malaysian Ringgit"),
    ("MZN", "Mozambique Metical"),
    ("NAD", "Namibia Dollar"),
    ("NGN", "Naira"),
    ("NIO", "Cordoba Oro"),
    ("NOK", "Norwegian Krone"),
    ("NPR", "Nepalese Rupee"),
    ("NZD", "New Zealand Dollar"),
    ("OMR", "Rial Omani"),
    ("PAB", "Balboa"),
    ("PEN", "Sol"),
    ("PGK", "Kina"),
    ("PHP", "Philippine Peso"),
    ("PKR", "Pakistan Rupee"),
    ("PLN", "Zloty"),
    ("PYG", "Guarani"),
    ("QAR", "Qatari Rial"),
    ("RON", "Romanian Leu"),
    ("RSD", "Serbian Dinar"),
    ("RUB", "Russian Ruble"),
    ("RWF", "Rwanda Franc"),
    ("SAR", "Saudi Riyal"),
    ("SBD", "Solomon Islands Dollar"),
    ("SCR", "Seychelles Rupee"),
    ("SDG", "Sudanese Pound"),
    ("SEK", "Swedish Krona"),
    ("SGD", "Singapore Dollar"),
    ("SHP", "Saint Helena Pound"),
    ("SLE", "Leone"),
    ("SOS", "Somali Shilling"),
    ("SRD", "Surinam Dollar"),
    ("SSP", "South Sudanese Pound"),
    ("STN", "Dobra"),
    ("SVC", "El Salvador Colon"),
    ("SYP", "Syrian Pound"),
    ("SZL", "Lilangeni"),
    ("THB", "Baht"),
    ("TJS", "Somoni"),
    ("TMT", "Turkmenistan New Manat"),
    ("TND", "Tunisian Dinar"),
    ("TOP", "Paʻanga"),
    ("TRY", "Turkish Lira"),
    ("TTD", "Trinidad and Tobago Dollar"),
    ("TWD", "New Taiwan Dollar"),
    ("TZS", "Tanzanian Shilling"),
    ("UAH", "Hryvnia"),
    ("UGX", "Uganda Shilling"),
    ("USD", "US Dollar"),
    ("UYU", "Peso Uruguayo"),
    ("UZS", "Uzbekistan Sum"),
    ("VED", "Bolívar Soberano"),
    ("VES", "Bolívar Soberano"),
    ("VND", "Dong"),
    ("VUV", "Vatu"),
    ("WST", "Tala"),
    ("XAF", "CFA Franc BEAC"),
    ("XCD", "East Caribbean Dollar"),
    ("XCG", "Caribbean Guilder"),
    ("XOF", "CFA Franc BCEAO"),
    ("XPF", "CFP Franc"),
    ("YER", "Yemeni Rial"),
    ("ZAR", "Rand"),
    ("ZMW", "Zambian Kwacha"),
    ("ZWG", "Zimbabwe Gold"),
)


# Set of valid codes for O(1) membership tests at validate-on-save time.
ISO_4217_CODES: frozenset[str] = frozenset(code for code, _ in ISO_4217_CURRENCIES)


def is_valid_currency_code(code: str) -> bool:
    """True if ``code`` (case-insensitive) is in the active ISO 4217 list."""
    return code.strip().upper() in ISO_4217_CODES


def currency_label(code: str) -> str:
    """Display label for a code — ``"GBP — Pound Sterling"``. Returns the
    bare code when not in the list (a legacy account that pre-dates the
    typeahead, or a non-standard code from a migration we haven't
    validated). The label is the dropdown's row text; the userData is
    still just the code."""
    u = code.strip().upper()
    for c, name in ISO_4217_CURRENCIES:
        if c == u:
            return f"{c} — {name}"
    return u
