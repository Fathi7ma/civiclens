"""City configuration and the Socrata dataset registry.

CivicLens is city-agnostic: swap CITY (and the dataset ids that point at
Austin-specific portals) to target another open-data city. Every fact the
engine reports is traceable to one of these datasets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass(frozen=True)
class Dataset:
    """A single Socrata dataset CivicLens is allowed to cite."""

    id: str
    name: str
    domain: str
    blurb: str
    primary_key: str
    #: Field -> human readable label, used when rendering facts
    labels: Dict[str, str] = field(default_factory=dict)
    #: Columns that are safe to expose in a citation detail view
    cite_fields: tuple = ()

    @property
    def api_base(self) -> str:
        return f"https://{self.domain}/resource/{self.id}.json"

    @property
    def portal_page(self) -> str:
        return f"https://{self.domain}/d/{self.id}"

    @property
    def portal_browse(self) -> str:
        return f"https://{self.domain}/api/views/{self.id}"


def _ds(**kwargs) -> Dataset:
    return Dataset(**kwargs)


def _austin_datasets() -> Dict[str, Dataset]:
    d = "data.austintexas.gov"
    return {
        "parcels": _ds(
            id="kk8y-6cmt",
            name="Land Database 2021 (parcels)",
            domain=d,
            blurb="Parcel boundaries with land use, unit counts and site area.",
            primary_key="prop_id",
            labels={
                "prop_id": "Parcel ID",
                "lu_desc": "Land use",
                "gen_lu_des": "Generalised land use",
                "shape_area": "Site area (sq ft)",
                "units": "Units on parcel",
                "ilr": "Improvement-to-land ratio",
                "employees": "Employees on site",
            },
            cite_fields=("prop_id", "lu_desc", "gen_lu_des", "units", "shape_area", "employees"),
        ),
        "zoning": _ds(
            id="nbzi-qabm",
            name="Zoning By Address",
            domain=d,
            blurb="Austin's official zoning designation for every address segment.",
            primary_key="objectid",
            labels={
                "full_street_name": "Address",
                "zoning_ztype": "Zoning designation",
                "base_zone": "Base district",
                "base_zone_category": "District category",
            },
            cite_fields=("full_street_name", "zoning_ztype", "base_zone", "base_zone_category"),
        ),
        "permits": _ds(
            id="3syk-w9eu",
            name="Issued Construction Permits",
            domain=d,
            blurb="Every issued building, electrical, mechanical and plumbing permit.",
            primary_key="permit_number",
            labels={
                "permit_number": "Permit number",
                "permit_type_desc": "Permit type",
                "description": "Scope of work",
                "issue_date": "Issued",
                "original_address1": "Site address",
                "council_district": "Council district",
                "contractor_company_name": "Contractor",
                "total_job_valuation": "Job valuation (USD)",
                "total_new_add_sqft": "New / added sq ft",
                "housing_units": "Housing units",
                "permit_class": "Permit class",
            },
            cite_fields=(
                "permit_number",
                "permit_type_desc",
                "description",
                "issue_date",
                "original_address1",
                "council_district",
                "total_job_valuation",
                "total_new_add_sqft",
                "housing_units",
                "permit_class",
            ),
        ),
        "service_requests": _ds(
            id="xwdj-i9he",
            name="Austin 311 Public Data",
            domain=d,
            blurb="Every 311 service request ever filed with the city.",
            primary_key="sr_number",
            labels={
                "sr_number": "Case number",
                "sr_type_desc": "Request type",
                "sr_department_desc": "Department",
                "sr_location": "Location",
                "sr_status_desc": "Status",
                "sr_created_date": "Filed",
                "sr_closed_date": "Closed",
                "sr_location_street_name": "Street",
                "sr_location_street_number": "Street number",
                "sr_location_council_district": "Council district",
            },
            cite_fields=(
                "sr_number",
                "sr_type_desc",
                "sr_department_desc",
                "sr_location",
                "sr_status_desc",
                "sr_created_date",
                "sr_closed_date",
                "sr_location_council_district",
            ),
        ),
        "council_districts": _ds(
            id="w3v2-cj58",
            name="Boundaries: City of Austin Council Districts",
            domain=d,
            blurb="Single-member council district boundaries (districts 1-10 in this dataset).",
            primary_key="district_number",
            labels={"district_name": "Council district", "council_district_path": "Contact"},
            cite_fields=("district_number", "district_name"),
        ),
        "floodplain": _ds(
            id="93kh-rg74",
            name="Greater Austin FEMA Floodplain",
            domain=d,
            blurb="National Flood Insurance Program flood-hazard polygons from FEMA.",
            primary_key="firm_panel",
            labels={
                "flood_zone": "FEMA flood zone",
                "firm_panel": "FIRM panel",
                "source_citation": "FEMA study",
                "effective_date": "Study effective",
            },
            cite_fields=("flood_zone", "firm_panel", "source_citation", "effective_date"),
        ),
        "neighborhoods": _ds(
            id="inrm-c3ee",
            name="Boundaries: City of Austin Neighborhoods",
            domain=d,
            blurb="Neighborhood Planning Areas and their planning status.",
            primary_key="planning_area_name",
            labels={
                "planning_area_name": "Neighborhood planning area",
                "neighborhood_planning_status": "Planning status",
                "neighborhood_number": "Neighborhood number",
            },
            cite_fields=("planning_area_name", "neighborhood_planning_status", "neighborhood_number"),
        ),
        "addresses": _ds(
            id="9s7j-tygf",
            name="Addresses",
            domain=d,
            blurb="Geocoded street address points used to resolve free-text locations.",
            primary_key="full_street_name",
            labels={"full_street_name": "Address", "street_name": "Street"},
            cite_fields=("full_street_name",),
        ),
        "historic": _ds(
            id="7c6r-hcmz",
            name="Historic Properties List",
            domain=d,
            blurb="Locally designated historic properties and their designation ordinance.",
            primary_key="dpb",
            labels={
                "property": "Property",
                "building_name": "Historic name",
                "ordinance_link": "Designation ordinance",
            },
            cite_fields=("dpb", "property", "building_name"),
        ),
    }


CITY: Dict[str, object] = {
    "id": "austin",
    "name": "City of Austin",
    "state": "Texas",
    "domain": "data.austintexas.gov",
    "portal": "https://data.austintexas.gov",
    "center": {"lat": 30.2672, "lon": -97.7431},
    "zoom": 12,
    "bbox": {"min_lat": 30.05, "max_lat": 30.55, "min_lon": -98.10, "max_lon": -97.35},
    "jurisdiction": "Austin Full Purpose",
    "app_token": os.getenv("SOCRATA_APP_TOKEN"),
    # Authoritative code the engine points to instead of reciting from memory.
    "code": {
        "development_code": "https://library.municode.com/tx/austin/codes/code_of_ordinances"
        "?nodeId=COOR_CH25ZO",
        "city_clerk": "https://www.austintexas.gov/cityclerk/codes",
        "permit_search": "https://abc.austintexas.gov/web/permit/public-search-others",
        "311_search": "https://www.austintexas.gov/page/311",
        "fema_firm": "https://msc.fema.gov/portal/home",
    },
}

DATASETS: Dict[str, Dataset] = _austin_datasets()


def dataset(key: str) -> Dataset:
    try:
        return DATASETS[key]
    except KeyError:
        raise KeyError(
            "Unknown dataset %r; known keys: %s" % (key, ", ".join(sorted(DATASETS)))
        )


def city_summary() -> Dict[str, object]:
    return {
        "id": CITY["id"],
        "name": CITY["name"],
        "state": CITY["state"],
        "portal": CITY["portal"],
        "center": CITY["center"],
        "zoom": CITY["zoom"],
        "bbox": CITY["bbox"],
        "jurisdiction": CITY["jurisdiction"],
        "code": CITY["code"],
        "datasets": [
            {
                "key": k,
                "id": v.id,
                "name": v.name,
                "blurb": v.blurb,
                "portal_page": v.portal_page,
                "primary_key": v.primary_key,
            }
            for k, v in sorted(DATASETS.items())
        ],
    }


def confidence_threshold() -> float:
    """Below this retrieval score the engine refuses instead of guessing."""
    return float(os.getenv("CIVICLENS_MIN_CONFIDENCE", "0.34"))


def http_timeout() -> float:
    return float(os.getenv("CIVICLENS_HTTP_TIMEOUT", "45"))


def cache_ttl() -> int:
    return int(os.getenv("CIVICLENS_CACHE_TTL", "600"))


def app_token() -> Optional[str]:
    return CITY.get("app_token") or None