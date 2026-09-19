"""Resolve lake names and optional metadata through the Wikidata API."""

from datetime import datetime, timezone
import re

import requests


WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
WIKIDATA_USER_AGENT = "PlanktonLab/1.0 (lake metadata lookup)"
LAKE_TYPE_IDS = {"Q23397", "Q131681", "Q3253281"}
LAKE_DESCRIPTION_PATTERN = re.compile(
    r"\b(lake|reservoir|pond|loch|lagoon|waterbody|body of water)\b",
    re.IGNORECASE,
)

METRE_UNITS = {
    "http://www.wikidata.org/entity/Q11573": 1.0,
    "http://www.wikidata.org/entity/Q828224": 1000.0,
}
SQUARE_KILOMETRE_UNITS = {
    "http://www.wikidata.org/entity/Q712226": 1.0,
    "http://www.wikidata.org/entity/Q25343": 0.000001,
}
CUBIC_KILOMETRE_UNITS = {
    "http://www.wikidata.org/entity/Q4243638": 1.0,
    "http://www.wikidata.org/entity/Q25517": 0.000000001,
}


class LakeLookupUnavailable(RuntimeError):
    """Raised when Wikidata cannot be reached or returns an invalid response."""


def _wikidata_request(params, timeout=8):
    try:
        response = requests.get(
            WIKIDATA_API_URL,
            params={**params, "format": "json", "origin": "*"},
            headers={"User-Agent": WIKIDATA_USER_AGENT},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.SSLError as error:
        raise LakeLookupUnavailable(
            "The secure connection to Wikidata was rejected. Check that your "
            "organization's certificate is installed in the system trust store, "
            "or enter the lake details manually."
        ) from error
    except requests.exceptions.Timeout as error:
        raise LakeLookupUnavailable(
            "Wikidata did not respond in time. Try again or enter the lake details "
            "manually."
        ) from error
    except (requests.RequestException, ValueError) as error:
        raise LakeLookupUnavailable(
            "Lake lookup is unavailable. Enter the lake details manually."
        ) from error
    if not isinstance(payload, dict) or payload.get("error"):
        raise LakeLookupUnavailable(
            "Lake lookup is unavailable. Enter the lake details manually."
        )
    return payload


def _claim(entity, property_id):
    statements = entity.get("claims", {}).get(property_id, [])
    statements = sorted(
        statements,
        key=lambda statement: statement.get("rank") == "preferred",
        reverse=True,
    )
    for statement in statements:
        snak = statement.get("mainsnak", {})
        if snak.get("snaktype") == "value":
            return snak.get("datavalue", {}).get("value")
    return None


def _entity_id(value):
    return value.get("id") if isinstance(value, dict) else None


def _quantity(entity, property_id, units):
    value = _claim(entity, property_id)
    if not isinstance(value, dict) or value.get("unit") not in units:
        return None
    try:
        return round(float(value["amount"]) * units[value["unit"]], 6)
    except (KeyError, TypeError, ValueError):
        return None


def _coordinate(entity):
    value = _claim(entity, "P625")
    if not isinstance(value, dict):
        return None, None
    try:
        return round(float(value["latitude"]), 7), round(float(value["longitude"]), 7)
    except (KeyError, TypeError, ValueError):
        return None, None


def _english_label(entity, fallback=""):
    return entity.get("labels", {}).get("en", {}).get("value", fallback)


def search_lakes(name, country=None, timeout=8):
    """Return Wikidata lake candidates and normalized, nullable metadata."""
    clean_name = " ".join((name or "").split())
    clean_country = " ".join((country or "").split())
    if len(clean_name) < 2:
        raise ValueError("Enter at least two characters of the lake name.")

    search_payload = _wikidata_request(
        {
            "action": "wbsearchentities",
            "search": clean_name,
            "language": "en",
            "uselang": "en",
            "type": "item",
            "limit": 10,
        },
        timeout,
    )
    entity_ids = [item.get("id") for item in search_payload.get("search", [])]
    entity_ids = [item_id for item_id in entity_ids if item_id]
    if not entity_ids:
        return []

    entities_payload = _wikidata_request(
        {
            "action": "wbgetentities",
            "ids": "|".join(entity_ids),
            "props": "labels|descriptions|claims",
            "languages": "en",
        },
        timeout,
    )
    entities = entities_payload.get("entities", {})

    referenced_ids = set()
    for entity in entities.values():
        for property_id in ("P17", "P31", "P131"):
            reference = _entity_id(_claim(entity, property_id))
            if reference:
                referenced_ids.add(reference)

    labels = {}
    if referenced_ids:
        labels_payload = _wikidata_request(
            {
                "action": "wbgetentities",
                "ids": "|".join(sorted(referenced_ids)),
                "props": "labels",
                "languages": "en",
            },
            timeout,
        )
        labels = {
            entity_id: _english_label(entity, entity_id)
            for entity_id, entity in labels_payload.get("entities", {}).items()
        }

    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = []
    for entity_id in entity_ids:
        entity = entities.get(entity_id, {})
        description = entity.get("descriptions", {}).get("en", {}).get("value", "")
        type_id = _entity_id(_claim(entity, "P31"))
        type_label = labels.get(type_id, "")
        if (
            type_id not in LAKE_TYPE_IDS
            and not LAKE_DESCRIPTION_PATTERN.search(description)
            and not LAKE_DESCRIPTION_PATTERN.search(type_label)
        ):
            continue

        country_id = _entity_id(_claim(entity, "P17"))
        country_label = labels.get(country_id, "")
        if clean_country and country_label.casefold() != clean_country.casefold():
            continue

        region_id = _entity_id(_claim(entity, "P131"))
        latitude, longitude = _coordinate(entity)
        depth = _quantity(entity, "P4511", METRE_UNITS)
        area = _quantity(entity, "P2046", SQUARE_KILOMETRE_UNITS)
        volume = _quantity(entity, "P2234", CUBIC_KILOMETRE_UNITS)
        warnings = []
        if depth and area and volume and (volume / area * 1000) > depth:
            area = None
            warnings.append(
                "Surface area was omitted because the source units were inconsistent."
            )
        results.append(
            {
                "lake": _english_label(entity, clean_name),
                "country": country_label,
                "description": description,
                "depth": depth,
                "area": area,
                "latitude": latitude,
                "longitude": longitude,
                "elevation_m": _quantity(entity, "P2044", METRE_UNITS),
                "mean_depth_m": None,
                "volume_km3": volume,
                "shoreline_length_km": _quantity(entity, "P2547", {
                    key: multiplier / 1000 for key, multiplier in METRE_UNITS.items()
                }),
                "catchment_area_km2": _quantity(
                    entity, "P2053", SQUARE_KILOMETRE_UNITS
                ),
                "residence_time_days": None,
                "waterbody_type": type_label or None,
                "administrative_region": labels.get(region_id) or None,
                "external_source": "Wikidata",
                "external_id": entity_id,
                "source_url": f"https://www.wikidata.org/wiki/{entity_id}",
                "metadata_retrieved_at": retrieved_at,
                "warnings": warnings,
            }
        )
    return results
