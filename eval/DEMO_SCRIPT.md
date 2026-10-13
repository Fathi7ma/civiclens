# CivicLens — 2:30 demo script

Two minutes thirty, no voiceover narration needed beyond the captions. Every
line below is a claim a judge can verify.

---

## 0:00 — The problem (12s)

**Screen:** Split text. Left: an AI chat bubble answering
*"can I build a duplex at 1507 Bouldin Ave?"* with
> "Yes! SF-3 zoning typically allows a second dwelling. Your setbacks would be
> **25 feet** rear and **10 feet** side, and you could probably get a **3.5**
> floor area ratio..."

Left, dimmed. Right: CivicLens answering the same question.

**Caption:** *Same question. One invents your setbacks. One shows you the record.*

---

## 0:12 — The flagship answer (35s)

**Screen:** CivicLens. Type slowly:

```
Can I build a duplex at 1507 Bouldin Ave?
```

Answer appears:

> **Can you build a duplex at 1507 BOULDIN AVE?**
> The zoning designation at 1507 BOULDIN AVE is **SF-3-NP** (Single Family
> residence district). **No.** A duplex is a second primary structure, and
> single-family base districts allow one primary dwelling per lot.

**Pan the chips:** Zoning designation `SF-3-NP` · Site area `0.09 acres` ·
Council district `District 9` · Flood zone `Zone X (shaded)`.

**Point at the caveat:**

> CivicLens reports the designation and the governing code; it deliberately does
> not recite setback, height or floor-area-ratio numbers from memory, because
> those are set by the district table and overlays in Development Code,
> Chapter 25-2.

**Caption:** *It just refused to state your setbacks. Here is why.*

---

## 0:47 — The receipts (35s)

**Screen:** The Evidence tab on the right. Click to expand the first citation.

Show the SoQL in full, then click **Run this query ↗** — the browser opens the
raw JSON from `data.austintexas.gov`.

**Caption:** *That is the exact query. Not a summary of it. Click and you run it yourself.*

Scroll the citation list: zoning, parcels, council district, neighborhood plan,
flood panel, permits. Six independent records, six clickable queries.

---

## 1:22 — It refuses (30s)

**Screen:** Type:

```
Can I build a duplex?
```

> **Can you build a duplex?** *I need an address before I can answer that*
> Whether you can build depends entirely on which parcel you are asking about,
> and the answer changes street to street.

Then:

```
What is my property tax assessment?
```

> **I can't answer that from city data.**
> CivicLens answers questions it can ground in the city's own records…
> It refuses everything else rather than inventing something plausible.

Then:

```
Who won the election in my district?
```

**Caption:** *A helpful assistant answers all three. These are the answers that cost people money.*

---

## 1:52 — It checks itself (25s)

**Screen:** Terminal.

```
$ python -m eval.run_eval
```

Scroll the run, then the summary:

```
pass rate            100%  (44/44)
intent classification 100%
grounded answers      100%
correct refusals      100%  (11 unanswerable cases)
```

**Screen:** `eval/benchmark.json` — show the `must_refuse: true` rows.

**Caption:** *44 questions. 11 of them are ones an AI should refuse. It gets all 44, and every answer has a record behind it.*

---

## 2:17 — Close (13s)

**Screen:** Back to the app. Drop a pin on the map. A new answer loads for that
exact location.

**Caption:**

> Every number CivicLens gives you is a query you can run.
> CivicLens — City of Austin.

**End card:** repo URL · live demo URL · `pip install -r requirements.txt && uvicorn civiclens.app:app`

---

## Shooting notes

**Prepare before recording.** Run `./run.sh`, then `python prewarm.py`, so no
answer is slow on camera. Clear the browser cache for a fresh first impression.

**Timing.** The flagship answer is cached and returns in ~10ms. Cold, it takes
2–5s while five open-data queries run in parallel — if a pause happens on a
fresh address, that is honest and fine, but prewarm the exact addresses you will
type.

**Font.** Bump the browser zoom to 125% so the citation SoQL is legible.

**Do not** cut away from a refusal. Refusing in front of the judge, on camera,
is the single most persuasive moment in the video.

**Backup recording.** Capture a second take with the browser dev tools open on
the Network tab showing `/api/ask` returning the citation URLs. It reads as
engineering rigour and takes 20 extra seconds.

## If asked live by judges

**"Does it use an LLM?"** No, and that's deliberate. It's deterministic, so it
structurally cannot invent a number. I can add an optional phrasing layer, but it
would make the prose smoother and the correctness identical.

**"What if the city's data is wrong or stale?"** Then it says so. Every record
carries its own identifier, and the historic-properties query returned nothing
for one test address — CivicLens reports that as a cited empty result rather than
assuming the answer is no.

**"How do you know the geospatial part is right?"** There's a test that
cross-checks the district polygon against the district field recorded
independently on permit rows: `tests/test_live.py::TestSpatialAccuracy`.

**"Is this only Austin?"** No. `CITY` in `config.py` plus the dataset ids.
The engine depends only on Socrata geometry, `group`/`count`, and text matching.