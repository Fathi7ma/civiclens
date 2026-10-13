# CivicLens

**Ask your city anything — and see the receipts.**

A civic answer engine for the City of Austin. Ask whether you can build a
duplex on your street, whether your parcel floods, what the city has actually
permitted there, whether anyone has reported potholes on your block. CivicLens
answers from the city's own open-data records, **cites the exact query behind
every claim**, and **refuses rather than guessing** when the data cannot support
an answer.

Built for [WarriorHacks 2.0](https://warriorhacks-2-0.devpost.com/).

---

## The problem

Ask a general-purpose AI *"can I build a duplex at 1507 Bouldin Ave?"* and it
will give you a confident answer. It will also confidently invent the setback
distance. There is no way to tell which parts came from a record and which
parts were generated, and the numbers it produces look exactly like the numbers
a person would act on.

The same is true in the other direction: ask *"who won the election in my
district?"* and a helpful assistant will answer, when the honest answer is "I
have no data on that."

The people most exposed to this are renters, first-time buyers and small
builders making expensive decisions on freely available public information that
nobody has made easy to read.

## What CivicLens does differently

| | A general-purpose LLM | CivicLens |
|---|---|---|
| Zoning designation | recalled from training data | queried from the live zoning record |
| Setbacks, height, FAR | invented or estimated | **never stated** — routed to the governing code section |
| "Who won the election?" | answered | refused, with the reason |
| Empty result | often filled with a plausible guess | cited as an empty query and explained |
| Wrong house number | answers about a different address anyway | says "not in the dataset, here's the nearest segment" |
| Every claim | unsourced | one click to the query that produced it |

The refusal is the feature. **An answer you can verify beats an answer that
sounds certain.**

---

## Quick start

```bash
cd civiclens
./run.sh                       # creates a venv, installs deps, serves on :8000
```

Then open <http://127.0.0.1:8000>.

No API key is required. All nine datasets are public on the City of Austin
open-data portal. An optional `SOCRATA_APP_TOKEN` only raises rate limits.

```bash
python -m pytest tests -q      # 96 tests: 79 offline, 17 live
python -m eval.run_eval        # 44-case benchmark, writes eval/results/
```

---

## How it works

```
                    ┌──────────────────────┐
  "Can I build a    │  classify()          │  intent + slots (year, radius,
   duplex at        │  engine.py           │  street, district, service type)
   1507 Bouldin     └──────────┬───────────┘
   Ave?"                        │
                    ┌──────────▼───────────┐
                    │  resolve()           │  geocode → real map point
                    │  resolve.py          │  (city address dataset,
                    └──────────┬───────────┘   Nominatim fallback)
                               │
              ┌────────────────┴────────────────┐
              │  5 enrichment queries, in       │
              │  parallel (ThreadPoolExecutor)  │
              └────────────────┬────────────────┘
                               │
                    ┌──────────▼───────────┐
                    │  builder per intent   │  answers.py — typed SoQL queries,
                    │  answers.py           │  deterministic composition
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  citations + conf.    │  every fact → reproducible
                    │  engine.py            │  query URL; refuse if unsupported
                    └──────────────────────┘
```

### The citation is the product

Each claim carries the URL of the query that produced it:

```
https://data.austintexas.gov/resource/nbzi-qabm.json
  ?$select=full_street_name,zoning_ztype,base_zone,base_zone_category,objectid
  &$where=upper(full_street_name) = '1507 BOULDIN AVE'
```

Open it and you run the same query yourself. Nothing is paraphrased from
memory, because nothing is paraphrased — the values are read straight out of
the response. A judge can audit any claim in one click.

### Grounding by construction

The engine is **deterministic**. No language model is involved in producing an
answer. A value appears in the response only if a query returned it, and the
query's URL is attached to it. There is no generation step that could invent a
number, which is why it does not need a guard against inventing numbers.

An optional LLM phrasing layer was deliberately left out. It would make the
prose smoother and the correctness identical, and it would reintroduce exactly
the failure mode the project exists to remove.

### Spatial strategy

The Austin Socrata instance does not expose `within_circle` or
`within_distance`. CivicLens therefore:

1. uses server-side `intersects(geom, 'POINT (lon lat)')` where it is available
   (council districts, neighbourhoods, parcels) — exact, indexed, fast;
2. falls back to a coarse `within_box` superset filtered by a locally computed
   point-in-polygon test with proper hole handling;
3. narrows point datasets (permits, 311) with a bounding box plus a local
   haversine filter;
4. **matches permits by site address text**, because most permit rows carry no
   coordinates and a radius filter silently drops them.

`tests/test_live.py::TestSpatialAccuracy` cross-checks the polygon district
lookup against the district field recorded independently on permit rows.

---

## Datasets

| Dataset | id | What it establishes |
|---|---|---|
| Land Database 2021 (parcels) | `kk8y-6cmt` | parcel boundary, land use, units, site area |
| Zoning By Address | `nbzi-qabm` | official zoning designation per address |
| Issued Construction Permits | `3syk-w9eu` | 2.4M permits: scope, valuation, dates, units |
| Austin 311 Public Data | `xwdj-i9he` | every service request, with create/close timestamps |
| Council Districts | `w3v2-cj58` | single-member district boundaries |
| Greater Austin FEMA Floodplain | `93kh-rg74` | flood-hazard polygons and FIRM panels |
| Neighborhoods (NPAs) | `inrm-c3ee` | neighborhood planning areas and status |
| Addresses | `9s7j-tygf` | geocoded address points (location resolution) |
| Historic Properties List | `7c6r-hcmz` | local historic designations |

---

## The refusal policy

CivicLens will not state dimensional standards — setbacks, maximum height, floor
area ratio, lot coverage. Those numbers are set by a district table in the
Development Code and changed by overlays and amendments. A chatbot reciting one
from training data is worse than useless to someone about to pour a slab.

Instead it identifies the district, names the code chapter that governs it, and
links it. See `kb.py` — the rationale is recorded there, not just asserted.

The benchmark deliberately includes 11 questions the system should decline,
including several a general-purpose model answers confidently:

| Question | Behaviour |
|---|---|
| "Can I build a duplex?" | asks which parcel |
| "Am I in a flood zone?" | asks for a location |
| "What is my property tax assessment?" | no appraisal dataset — declines |
| "Who won the election in my district?" | out of scope — declines |
| "What should I do about a pothole?" | advice, not a record — declines |

---

## Benchmark

`python -m eval.run_eval --markdown`

| metric | result |
|---|---|
| overall pass rate | **100%** (44/44) |
| intent classification | 100% |
| answers grounded in the required datasets | 100% |
| correctly refused unanswerable questions | 100% (11/11) |

A case passes only when the intent matches, the answer/refuse decision matches,
**and** every required dataset appears in the citations. An answer that reaches
the right headline without a citable record behind it does not pass.

`python -m eval.baseline_llm` runs the same questions through a
retrieval-free LLM (with `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`) and checks its
stated zoning designations against the live record, so the anti-hallucination
claim is measured rather than asserted. It needs a key; the benchmark above
does not.

---

## API

```bash
curl -s -X POST localhost:8000/api/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"Can I build a duplex at 1507 Bouldin Ave?"}'
```

```jsonc
{
  "intent": "zoning_feasibility",
  "headline": "Can you build a duplex at 1507 BOULDIN AVE?",
  "confidence": 0.734,
  "confidence_label": "medium",
  "refused": false,
  "facts": [
    {"label": "Zoning designation", "value": "SF-3-NP"},
    {"label": "Site area",          "value": "0.09 acres", "note": "4011 sq ft"}
  ],
  "citations": [
    {"dataset_key": "zoning", "record_id": "3096905", "query_url": "https://…"}
  ],
  "data_gaps": [],
  "follow_ups": ["What permits have been issued at 1507 BOULDIN AVE?"]
}
```

| Endpoint | Purpose |
|---|---|
| `POST /api/ask` | ask a question (`question`, optional `lat`/`lon`) |
| `POST /api/locate` | geocode an address to a sourced point |
| `GET  /api/city` | city config and dataset registry |
| `GET  /api/examples` | seed questions |
| `GET  /api/health` | liveness and upstream stats |
| `GET  /api/eval/report` | last benchmark report |

---

## Layout

```
civiclens/
  config.py      city config + dataset registry (single source of truth)
  socrata.py     citation-first Socrata client, disk-backed TTL cache
  geo.py         point-in-polygon, haversine, address normalisation
  resolve.py     location resolution and the five enrichment lookups
  intents/       (in engine.py) weighted intent classification
  zoning.py      zoning designation structure, without dimensional guesses
  kb.py          FEMA glossary, district categories, code routing
  retrieve.py    in-repo BM25 over the knowledge base
  answers.py     one answer builder per intent
  engine.py      orchestration, citations, confidence
  app.py         FastAPI
  static/        single-page UI, no build step
eval/            benchmark, scorer, optional LLM baseline
tests/           79 offline unit tests + 17 live integration tests
```

## Extending it to another city

Change `CITY` in `config.py` and repoint the dataset ids in `_austin_datasets()`.
The engine itself is city-agnostic: it relies only on Socrata geometry,
`$group`/`count`, and text matching. A city without a zoning-by-address dataset
will simply report that gap — which is the correct behaviour.

---

## Design notes worth calling out

- **A negative result is a cited result.** "No historic designation on record"
  carries the lookup query, so the absence is verifiable rather than asserted.
- **Corridor matches are disclosed and confidence-capped.** Asking about an
  address the geocoder lacks returns the nearest segment, says so, and caps
  confidence at 0.55.
- **Truncation is disclosed.** A street with 1,091 permits shows the 200 most
  recent and says how many were omitted, after a separate `count` query.
- **Refusal is a designed path, not an error path.** Every builder has an
  explicit refusal branch that explains what is missing and what would fix it.

## Licence

MIT. Map tiles © OpenStreetMap contributors. City data is public domain.