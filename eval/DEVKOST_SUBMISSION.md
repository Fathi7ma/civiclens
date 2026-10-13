# Devpost submission — paste-ready

Copy each block into the matching field on the Devpost submission form.

---

## 1. Submission title (tagline)

```
CivicLens — ask your city anything, and see the receipts
```

## 2. One-line subtitle / short description

```
A civic answer engine grounded in 5.9 million official City of Austin records.
Every claim links to the exact query that produced it — and it refuses to
answer rather than inventing your setbacks.
```

---

## 3. About the project (main description)

Paste this into the **About the project** field. Devpost accepts Markdown.

```markdown
> **Ask your city anything — and see the receipts.**

## The problem

The City of Austin publishes **5,956,599 records** about its own land, zoning,
building, and street life. Parcels. Zoning designations. Building permits. Every
311 request a resident has ever filed. Flood maps. Council districts.

Almost nobody outside the planning department can read them.

So people ask the one tool they have — an AI chatbot. And it answers.
Confidently. With specific numbers.

Ask it *"can I build a duplex at 1507 Bouldin Ave?"* and it will tell you your
setback distance. It will not tell you where that number came from, because it
doesn't come from anywhere. It was generated.

Someone who trusts it pours a slab.

The same failure runs the other way: ask *"who won the election in my
district?"* and a helpful assistant answers, rather than admitting it has no
data on that.

## What CivicLens does

A question-answering engine over the city's actual open-data portal. Ask about
a parcel, a street, or the whole city:

| Question | What you get |
|---|---|
| "Can I build a duplex at 1507 Bouldin Ave?" | Live zoning designation, district category, what that does and does not permit |
| "Is 1507 Bouldin Ave in a floodplain?" | FEMA zone, risk class, FIRM panel — with the meaning of the code |
| "What permits have been issued on Bouldin Ave?" | 1,091 permits, declared job value, floor area, units authorised |
| "What 311 complaints are open near here?" | Case counts, median days to close, still-open tally |
| "Which districts get the most 311 requests?" | Ranked aggregate from one citable query |
| "Who won the election in my district?" | *"I can't answer that from city data."* |

## The citation is the product

Every fact in every answer carries the **exact SoQL query** that produced it:

```
https://data.austintexas.gov/resource/nbzi-qabm.json
  ?$select=full_street_name,zoning_ztype,base_zone,base_zone_category,objectid
  &$where=upper(full_street_name) = '1507 BOULDIN AVE'
```

Click it. You run the same query yourself against the city's portal and see the
raw JSON. Nothing is paraphrased, because nothing is paraphrased — the values
come straight out of the response.

If a claim cannot be traced to a record, it does not appear.

## It refuses. That's the feature.

**CivicLens will not state setbacks, maximum height, floor area ratio, or lot
coverage.** Those numbers are set by a district table in the Development Code
and changed by overlays and amendments. A chatbot reciting one from training
data is worse than useless to someone about to pour concrete.

So CivicLens identifies the district, names the code chapter that governs it,
and links it. It tells you where to look rather than guessing what it says.

It also refuses when a record simply doesn't exist, and says so *with the
citation*:

> **No local historic designation on record for 1507 BOULDIN AVE**
> The city's historic properties list returned no record for that address, and
> the lookup is cited above so you can confirm it.

A missing permit doesn't prove nothing was built. An empty result is reported
as an empty result, with the query.

## How it works

```
question
  → intent + slot extraction        (year, radius, street, district, service type)
  → geocode to a real map point     (city address dataset, Nominatim fallback)
  → 5 enrichment queries in parallel (parcel, zoning, district, NPA, flood)
  → intent-specific answer builder  (typed SoQL, deterministic composition)
  → citations + confidence score    (refuse if the data cannot support it)
```

**No language model produces the answer.** A value appears only if a query
returned it, and that query's URL is attached to it. There is no generation
step that *could* hallucinate — which is why the refusal policy is structural
rather than a guardrail bolted on afterwards.

An optional LLM phrasing layer was deliberately left out. It would make the
prose smoother and the correctness identical, and it would reintroduce exactly
the failure this project exists to remove.

**Geospatial.** Austin's Socrata instance doesn't expose `within_circle` or
`within_distance`, so CivicLens uses server-side `intersects(geom, 'POINT(...)')`
where available, and falls back to a coarse `within_box` filtered by a locally
computed point-in-polygon test with proper hole handling. Permits are matched by
site-address text, because most permit rows carry no coordinates and a radius
filter silently drops them.

## Evidence

**96 automated tests** — 79 offline unit tests, 17 live integration tests
(`tests/`).

**44-case benchmark** (`python -m eval.run_eval`):

| metric | result |
|---|---|
| overall pass rate | **100%** (44/44) |
| intent classification | 100% |
| answers grounded in the required datasets | 100% |
| correctly refused unanswerable questions | 100% (11/11) |

A case passes only when the intent matches, the answer/refuse decision matches,
**and** every required dataset appears in the citations. An answer that reaches
the right headline without a citable record behind it does not pass.

11 of the 44 cases are questions the system must decline — several of which a
general-purpose model answers confidently. Those are the ones that matter.

`python -m eval.baseline_llm` runs the same questions through a retrieval-free
LLM and checks its stated zoning designations against the live record, so the
anti-hallucination claim is **measured rather than asserted**.

One live test cross-checks the district polygon against the district field
recorded independently on permit rows.

## Who it's for

Renters checking whether a unit is legal. First-time buyers deciding whether to
bid. Neighbours comparing street-by-street flood exposure before insurance
renewal. Small builders pricing a job. Local journalists verifying a claim.
Anyone who has ever been confidently wrong by an AI about their own address.

## Built with

Python 3.9 · FastAPI · Socrata SODA API · Leaflet · OpenStreetMap ·
in-repo BM25 · vanilla JS (no build step) · pytest

No API keys. No LLM. No vector database. ~5,700 lines of Python, ~1,300 of
frontend.

## What's next

- Appraisal and tax data — the most-asked question CivicLens currently declines
- Parcels-by-description and permit-status lookups
- Austin → other cities (`CITY` in `config.py` plus dataset ids; the engine
  depends only on Socrata geometry, `group`/`count`, and text matching)
- A "what changed since last year" diff view, which residents ask for constantly
```

---

## 4. Built With (checkboxes / tags)

Tick these on the form:

```
Python
FastAPI
Uvicorn
REST API
JSON
Leaflet
JavaScript
HTML5/CSS3
PostgreSQL
Open Data
Socrata
API
Testing
pytest
```

If the form offers free-text tags instead:

```
python, fastapi, rest-api, open-data, socrata, leaflet, openstreetmap,
vanilla-js, no-build-step, retrieval, grounded-generation, bm25, geospatial,
civic-tech, open-government, pytest, no-api-key-required, deterministic
```

## 5. Links

| Field | What to paste |
|---|---|
| **Project URL** | your live deployment (see `render.yaml` — free tier, one click) |
| **Repository URL** | `https://github.com/<you>/civiclens` — **must be public** |
| **Video / Demo URL** | your unlisted YouTube link, or the Devpost video upload |
| **TryHackOut / Other** | leave blank |

## 6. Screenshots — capture exactly these four

Devpost shows these above the fold. Capture them at **1280×800 or wider**, in a
browser at **125% zoom**, with the light/dark theme that reads best.

1. **The flagship answer** — "Can I build a duplex at 1507 Bouldin Ave?" with the
   fact chips and the confidence badge visible. This is your hero image.
2. **The citations panel** — Evidence tab, first citation expanded, showing the
   full readable SoQL and the "Run this query ↗" link. *This is the image that
   proves the product.* Name it `citations.png`.
3. **A refusal** — "What is my property tax assessment?" with the honest
   "I can't answer that from city data" and the follow-up chips.
4. **The map + markers** — ask about 311 complaints near an address so the red
   open / green closed case markers render across Bouldin Ave. Shows it's live
   geospatial data, not a mockup.

Optionally a fifth: the terminal showing
`pass rate 100% (44/44)` from `python -m eval.run_eval`.

## 7. Track selection

- **Hackathon track** — the Devpost form and the separate
  [Track Selection Form](https://docs.google.com/forms/d/e/1FAIpQLScYxGmIowiwkZsbmJwWhoc5kPi2_gbJAgeGBAuoMiNPPD_kbA/viewform)
  are **both** required. Submitting the repo does not replace the form.
- Required for the hackathon track: public GitHub repo ✅ · 2–3 minute demo
  video · screenshots · demo link (recommended)

## 8. Final checklist

- [ ] Repo is **public**, `README.md` renders correctly
- [ ] `git init && git commit && git push` — nothing is committed yet
- [ ] Deployed and the live URL loads
- [ ] `python3 prewarm.py` run so demo answers are instant
- [ ] Video recorded using `eval/DEMO_SCRIPT.md` (the 1:22 refusal segment is
      the strongest moment — don't cut it)
- [ ] Track Selection Form submitted
- [ ] Screenshots 1–4 uploaded