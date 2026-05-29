"""ITU-R P.837 rain climate zones.

Rain rate exceeded at a given fraction of an average year, by climate zone.
Used to size link-health buckets: a link must survive its local 99.99 % rate
to be GREEN, 99.9 % to be YELLOW.
"""

from typing import Optional

# Percentage-of-time labels matching ITU-R P.837 columns. Lower percentages
# correspond to more extreme storms.
EXCEEDANCE_LABELS = ("1%", "0.3%", "0.1%", "0.03%", "0.01%", "0.003%", "0.001%")

# Rain rate (mm/hr) per climate zone at each exceedance percentage above.
# Values from ITU-R P.837 Table 1 (standard zones A-Q).
RAIN_RATES_BY_ZONE: dict[str, dict[str, float]] = {
    zone: dict(zip(EXCEEDANCE_LABELS, rates))
    for zone, rates in {
        "A": (0.099, 0.8, 2,  5,   8,   14,  22),
        "B": (0.5,   2,   3,  6,   12,  21,  32),
        "C": (0.7,   2.8, 5,  9,   15,  26,  42),
        "D": (2.1,   4.5, 8,  13,  19,  29,  42),
        "E": (0.6,   2.4, 6,  12,  22,  41,  70),
        "F": (1.7,   4.5, 8,  15,  28,  54,  78),
        "G": (3,     7,   12, 20,  30,  45,  65),
        "H": (2,     4,   10, 18,  32,  55,  83),
        "J": (8,     13,  20, 28,  35,  45,  55),
        "K": (1.5,   4.2, 12, 23,  42,  70,  100),
        "L": (2,     7,   15, 33,  60,  105, 150),
        "M": (4,     11,  22, 40,  63,  95,  120),
        "N": (5,     15,  35, 65,  95,  140, 180),
        "P": (12,    34,  65, 105, 145, 200, 250),
        "Q": (24,    49,  72, 96,  115, 142, 170),
    }.items()
}

# Default when geolocation is unknown or the country isn't mapped. K is a
# middle-of-the-road continental climate (US/most of Europe at country grain).
DEFAULT_ZONE = "K"

# ISO 3166-1 alpha-2 country code → predominant ITU-R P.837 zone.
# This is a country-grain approximation; per-site refinement from lat/lng is
# out of scope for v1.
COUNTRY_TO_ZONE: dict[str, str] = {
    # North America
    "US": "K", "CA": "E", "MX": "N",
    # Central America & Caribbean
    "GT": "N", "HN": "N", "NI": "N", "CR": "N", "PA": "N",
    "CU": "N", "DO": "N", "PR": "N", "JM": "N", "HT": "N",
    # South America
    "BR": "N", "AR": "K", "CL": "E", "CO": "P", "PE": "N",
    "VE": "N", "EC": "P", "BO": "K", "PY": "K", "UY": "K",
    # Europe
    "GB": "E", "IE": "E", "FR": "E", "DE": "E", "NL": "E",
    "BE": "E", "LU": "E", "CH": "E", "AT": "E", "IT": "H",
    "ES": "H", "PT": "H", "GR": "H", "PL": "E", "CZ": "E",
    "SK": "E", "HU": "E", "RO": "E", "BG": "E", "HR": "E",
    "SI": "E", "RS": "E", "BA": "E", "AL": "H", "MK": "E",
    "DK": "E", "SE": "E", "NO": "E", "FI": "E", "IS": "B",
    "EE": "E", "LV": "E", "LT": "E", "BY": "E", "UA": "E",
    "RU": "E", "MD": "E",
    # Middle East / North Africa
    "TR": "K", "IL": "C", "PS": "C", "JO": "B", "LB": "K",
    "SY": "C", "IQ": "C", "IR": "C", "SA": "C", "AE": "C",
    "OM": "C", "YE": "C", "KW": "C", "QA": "C", "BH": "C",
    "EG": "C", "LY": "B", "TN": "E", "DZ": "B", "MA": "E",
    # Sub-Saharan Africa
    "NG": "P", "GH": "N", "CI": "N", "SN": "N", "ML": "K",
    "ET": "K", "KE": "N", "TZ": "N", "UG": "N", "RW": "N",
    "ZA": "K", "ZM": "N", "ZW": "K", "BW": "K", "NA": "K",
    "CD": "P", "CG": "P", "CM": "P", "GA": "P", "AO": "N",
    "MZ": "N", "MG": "N",
    # Asia
    "IN": "N", "PK": "N", "BD": "P", "LK": "P", "NP": "N",
    "BT": "N", "MM": "P", "TH": "P", "VN": "P", "LA": "P",
    "KH": "P", "MY": "P", "SG": "P", "ID": "P", "PH": "P",
    "BN": "P", "TL": "P", "CN": "K", "HK": "N", "TW": "N",
    "JP": "M", "KR": "M", "KP": "M", "MN": "E",
    "KZ": "E", "UZ": "E", "TM": "B", "KG": "E", "TJ": "E", "AF": "B",
    # Oceania
    "AU": "K", "NZ": "E", "PG": "P", "FJ": "P", "SB": "P", "VU": "P",
    "NC": "P", "PF": "N", "WS": "P", "TO": "N", "KI": "P", "MH": "P",
    "FM": "P", "PW": "P", "MP": "P", "GU": "P", "AS": "P",
}


def zone_for_country(country_code: Optional[str]) -> str:
    """Return the ITU-R P.837 climate zone for an ISO country code.

    Falls back to DEFAULT_ZONE when the country is unknown or unmapped.
    """
    if not country_code:
        return DEFAULT_ZONE
    return COUNTRY_TO_ZONE.get(country_code.upper(), DEFAULT_ZONE)


def rates_for_zone(zone: str) -> dict[str, float]:
    """Return the rain-rate dict (mm/hr per exceedance %) for a zone.

    Falls back to DEFAULT_ZONE when the zone id is unknown.
    """
    return RAIN_RATES_BY_ZONE.get(zone, RAIN_RATES_BY_ZONE[DEFAULT_ZONE])
