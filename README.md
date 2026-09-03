# Hardware Agent

A component sourcing search tool. Type a part number, a category or a plain
description — `STM32F103C8T6`, `Artix-7 FPGA`, `10k 0805 resistor` — and every
configured distributor is queried **in parallel**, then merged into one table:

| Component | Supplier | Available stock | Required stock | Unit price | Total price | Link |
|---|---|---|---|---|---|---|

Each row links straight through to that supplier's own product page.

## Running it

Python 3.8+ is the only requirement. There are **no third-party packages** — it
runs on the standard library alone, so there is nothing to install.

```
python app.py
```

Then open <http://127.0.0.1:8080> (it opens automatically). On Windows you can
also double-click `run.bat`; on macOS/Linux use `./run.sh`.

### Why the same file used to total differently

Uploading one BOM twice could give two different totals. Three things combined
to cause it, and all three are fixed:

- A rate-limited supplier dropped out of a line, so whoever *did* answer became
  that line's winner. Which suppliers got throttled varied run to run, so the
  chosen supplier — and the price — varied with it.
- Those incomplete results were cached as though complete, freezing a missing
  supplier in for the life of the entry.
- The cache expired after 60 seconds, less than a long BOM takes to run, so a
  repeat upload re-queried everything and rolled the dice again.

Now only a complete answer is cached, the cache holds long enough for a repeat
run to reuse it, and a second pass re-prices any line a supplier failed on —
by then the rate limit has cleared, so the retry usually gets it. Lines that
stay incomplete say so rather than quietly picking a winner from whoever
answered.

## Watching the API quota

Every search costs one call per supplier, and a BOM costs one per line per
supplier — so a 58-line file is about 174 calls in a few seconds. Run out
part-way through and the damage is quiet: the early lines price, the rest come
back unpriced, and the total is wrong without looking wrong.

Open the sources drawer and each supplier shows what it has spent today.
Digi-Key reports its own allowance in a response header, so its figure is
exact and says so. Mouser and element14 report nothing at all, so the app
counts calls itself — a floor rather than a total, since it cannot see calls
made from another machine sharing the same key. Put your dashboard figure in
`MOUSER_DAILY_LIMIT` / `FARNELL_DAILY_LIMIT` to get a bar for those too.

The tally lives in `.usage.json` and survives restarts, which matters: a count
held in memory would read as plenty of quota left on the day you restarted
most. It resets on the date changing.

Before a BOM starts, the remaining allowance is checked against the number of
lines. If it cannot finish, pricing stops before spending anything and says
which supplier is short — you can still choose to run it and take prices from
the others.

## Letting other people use it

By default the app listens on `127.0.0.1`, which means this machine and nothing
else — sharing that URL cannot work, because `127.0.0.1` resolves to whatever
computer it is typed on.

**On your own network.** Set `HOST=0.0.0.0` in `.env` and restart. The banner
then prints the address to share, e.g. `http://10.100.100.138:8080/`. Guest and
corporate Wi-Fi often block device-to-device traffic, in which case this will
not work however it is configured.

**Over the internet.** Run `share.bat`. It starts the app and a Cloudflare
tunnel, and the tunnel window prints an `https://....trycloudflare.com` address
that works from anywhere. The tunnel dials out from this machine, so it needs
no port forwarding and works from behind guest Wi-Fi. Requires
`winget install --id Cloudflare.cloudflared` once.

The link lives only as long as both windows stay open, and a new address is
issued each restart. A fixed address needs a domain on a Cloudflare account.

**Set a password first.** There are no user accounts; `APP_PASSWORD` in `.env`
is one shared password covering the whole app, and with it empty anyone who
finds the URL can search, price BOMs and spend the distributor API quota your
keys are paying for. Set it, restart, and the banner confirms `Password on`.
Sessions last a week and every restart of the server signs everyone out.

## Getting real-time data

This is the part that needs your attention.

Real-time distributor pricing comes from **official distributor APIs**. Scraping
distributor websites directly does not work in practice: Digi-Key, Mouser,
Farnell and the rest sit behind bot protection that blocks automated requests, and doing
it anyway would break constantly and violate their terms. So this app talks to
the documented APIs instead, and each one activates as soon as you supply a key.

Every source needs a key, so until you supply at least one the app falls back
to its offline reference catalogue and labels those figures as indicative.

To get live results, copy `.env.example` to `.env`, add whichever keys you
have, and restart:

| Source | What you get | Where the key comes from | Cost |
|---|---|---|---|
| **Nexar / Octopart** | Offers from Digi-Key, Mouser, Arrow, TME and more in a single call | <https://portal.nexar.com> | Free tier |
| **Mouser** | Mouser stock and price breaks | <https://www.mouser.com/api-hub/> | Free, ~1000 calls/day |
| **Digi-Key** | Digi-Key stock and price breaks | <https://developer.digikey.com> | Free |
| **Farnell / element14 / Newark** | Regional element14 storefront | <https://partner.element14.com> | Free |

**Configure Nexar first if you only configure one** — it is an aggregator, so a
single key gives you most of the market at once.

A `.env` file is already created for you with every setting in place; you only
need to paste the value after the `=`. After pasting, verify it before
restarting:

```
python tools/check_keys.py
```

That runs a real search against each configured distributor and tells you
exactly what came back — including what a rejection means, e.g. *"MOUSER_API_KEY
is malformed. It should be a UUID."* Then restart with `python app.py`.

If no live source can answer at all, the app falls back to a built-in reference
catalogue of 187 parts spanning the full category tree below. Those rows are
generated locally and are **not live quotes**, so every one is badged `SAMPLE`.
It defaults to `ENABLE_DEMO=auto`, which stands the catalogue down the moment a
live source is available — generated figures never share a table with real
ones.

You can always see exactly which sources are live: the status pills under the
search bar report `ok` / `empty` / `error` / `disabled` per distributor for every
query, and the **Data sources** button in the header explains what each one
needs.

## Category search

The agent knows a component category tree of **988 categories in 67 groups**,
covering roughly 12.5 million distributor line items.

It comes from two places. A hand-written tree goes **deep** on the
semiconductor taxonomy: amplifiers down to the leaf level (op amps,
instrumentation, transimpedance, LNAs, log amps, sample & hold, video, 4-20mA
conditioners...), plus data converters, power management, motor drivers,
interface, isolation, logic, memory and FPGA/CPLD -- and it carries the 187
reference part numbers behind the offline catalogue. A generated catalogue goes
**wide**: everything else a distributor actually stocks -- resistors,
capacitors, inductors, crystals, relays, switches, connectors, cable, circuit
protection, enclosures, thermal management, motors, power supplies, sensors,
test gear, prototyping supplies.

That means you can search the way an engineer actually thinks:

- Search `transimpedance amplifiers` and the agent recognises the category,
  shows you how it interpreted the query (`Amplifiers > Special function
  amplifiers > Transimpedance amplifiers`) and sends every distributor a clean
  canonical keyword rather than your raw phrase.
- Search a parent like `op amps` and it reaches into the child branches too, so
  you get audio, general-purpose, high-speed, power and precision parts.
- Click **Browse categories** under the search bar to pick from the whole tree.
- Exact part numbers are never touched: `OPA855IDSGT` stays a part-number
  lookup, no category rewriting.

- The **Find a category** box inside the browser filters the whole tree, so you
  do not have to scroll 988 entries to reach `Ferrite Beads and Chips`.

The hand-written tree lives in `agent/taxonomy.py`; adding a branch there
extends search, the category browser and the offline catalogue at once. The
generated half lives in `agent/catalogue.py` -- do not edit it by hand, run:

```
python tools/fetch_categories.py
```

That pulls the current tree from Digi-Key's Product Information v4 category
endpoint, the only one of the configured suppliers that publishes a
machine-readable taxonomy (Mouser's Search API and element14's catalog API
expose no category listing). The names are Digi-Key's, but they are used as
search keywords against **every** configured supplier, exactly as the
hand-written categories are -- so the broader tree broadens the search
everywhere, not just at Digi-Key. Where the two trees name the same category
the hand-written node wins, because it has tuned aliases and real part numbers
behind it; anything genuinely new underneath is grafted on.

Each generated category also carries the keyword actually worth sending a
distributor. A distributor's own browse names are built for a faceted tree, not
a search box -- `Linear - Amplifiers - Instrumentation, OP Amps, Buffer Amps`
finds nothing typed literally -- so the tree stores a cleaned term alongside the
name, and the breadcrumb tells you which one went upstream.

## Identify a component from a photo

Click **Identify from photo** under the search bar, drop in a picture of the
part (or paste one with `Ctrl`+`V`), and a vision model running locally through
[Ollama](https://ollama.com) reads the package markings and names the
component. The part it identifies is then priced across every configured
supplier automatically — one step from "what is this chip?" to a stock and
price table.

The photo is sent to Ollama on **this machine** over localhost and nowhere
else, so pictures of unreleased hardware never reach a third party.

### Setting it up

Install Ollama, then pull a model that can read images — a plain text model
like `llama3` cannot:

```
ollama pull qwen2.5vl:3b     # best at reading small printed markings, ~3 GB
ollama pull moondream        # smallest, ~1.7 GB, weaker at text
ollama pull llava:7b         # ~4.7 GB
```

That is all. The agent probes Ollama at start-up, picks the best installed
vision model on its own, and shows what it found on the panel's status pill.
The **Re-check Ollama** button picks up a model you pull while the server is
running. To pin a specific one, set `OLLAMA_VISION_MODEL` in `.env`; the other
settings (`OLLAMA_HOST`, `OLLAMA_TIMEOUT`, `OLLAMA_KEEP_ALIVE`, and
`ENABLE_VISION=false` to switch the feature off) are documented in
`.env.example`.

### What you get back

Each candidate card carries the part number the model read, the package, the
raw markings, the manufacturer, a confidence figure and the reasoning — plus
the category the guess snapped onto in the built-in taxonomy, which is what
gets sent to the distributors. The top candidate is searched immediately; the
others are one click away if the model picked the wrong part.

### How it avoids inventing parts

A 3B vision model will happily produce a confident, plausible, completely wrong
part number for a package it cannot read. Three guards sit around it:

1. **Reading is separated from identifying.** The photo goes through an OCR
   pass first, with identification explicitly forbidden -- an engine asked to do
   both at once lets the identification steer the reading, deciding the part is
   a TMS320 and then "reading" markings that agree. The transcription is then
   quoted back to the identification pass as ground truth.
2. **Every proposed part number is looked up at the live distributors.** A part
   nobody stocks is almost always one the model made up, and it is demoted and
   labelled as such. Only an exact match counts -- a reel-suffix variant is a
   different orderable part, so it does not prove the searched part exists.
3. **The package wins over the model.** Where the string printed on the part
   checks out at a supplier and the model's own reading does not, the printed
   string is what gets searched, and the card shows both.

What the OCR pass actually read is printed verbatim above the candidates, so
the one thing you can check against the part in your hand in a second is right
there. A candidate the agent cannot stand behind is not auto-searched at all.

Both guards cost time -- roughly 27 s per photo warm instead of 19 s -- and both
can be turned off with `VISION_OCR_PASS` and `VISION_VERIFY` in `.env`.

Two more things worth knowing:

- **It is a reading, not a fact.** Top-line markings on small packages are
  abbreviated codes, and a model can misread a blurred character. Confirm the
  part number against the markings and the datasheet before you order.
- **Where no part number is legible**, the candidate is searched as a
  description (`10k 0805 resistor`), so you get a family of parts rather than
  one exact match. The card says so explicitly.

Photos are downscaled to 1280 px in the browser before they are sent, because
a 12 MP phone shot carries no more legible detail of a chip top than a 1280 px
crop and costs the model far more time. Expect a cold first run to take
noticeably longer than later ones — that one includes loading the model into
memory.

## Price a whole bill of materials

Click **Price a BOM file** under the search bar and drop in your BOM as
`.xlsx`, `.csv` or `.pdf`. The agent finds the manufacturer part number and
quantity columns, prices every line across every configured supplier, and gives
the finished table back as **Excel**.

### Reading the file

No two BOMs are laid out the same way, so three strategies run in order and the
first one that actually works on the data wins:

1. **Column headings.** `MPN`, `Mfr Part #`, `Manufacturer Part Number`, `QTY/BOARD`
   and the rest are matched most-specific-first, so a sheet carrying both an
   internal part number and the manufacturer's picks the manufacturer's — only
   that one is orderable at a distributor. Reference-designator, footprint and
   price columns are explicitly barred from being mistaken for the part number.
2. **The local model.** If the headings are missing or misleading, the Ollama
   model is asked which column holds part numbers and which holds quantities.
   Its answer is two column indexes, and those are checked against the data
   before they are used.
3. **The column contents.** Failing both, the column with the most
   part-number-shaped cells wins, and the column of small positive integers
   beside it becomes the quantity.

Once the column is known, extraction is deliberately **permissive**. Real
catalogues are full of part numbers that break every rule of thumb -- Wurth's
`885012005033` has no letters at all, Molex's `53261-0571` is digits and a
hyphen, Hirose ships `DF40HC(4.0)-50DS-0.4V(51)` -- so a cell is only rejected
when it cannot be a part number: prose, a placeholder like `DNP`, a footer
label, a date, or a bare number short enough to be a line count. Anything that
is rejected is **listed back to you** with an "Add them anyway" button, so a row
is never silently lost.

### Quantities, and the build size

A quantity on a BOM is never just a number. A sheet built for a batch carries
the same part twice over — `QTY` is what one board needs, `QTY Of 5 Units` is
what the whole batch needs — and reading either one as "the quantity" is wrong
half the time. Order the first and five boards' worth of parts never arrive;
order the second and a request for twenty boards still buys five.

So the agent never takes a quantity at face value. Every quantity column is
reduced to a **per-unit** figure plus the build size it was written for, and the
order quantity is then per-unit × however many units you actually want. That is
the only form that survives the requirement changing, which it always does:
**"Units to build"** sits above the table, and typing `20` into it restates
every line instantly.

Three things establish the build size, in descending order of trust
(`agent/quantities.py`):

1. **Ratios between columns.** When one column is an exact integer multiple of
   another across the whole sheet, that multiple *is* the build size. This is
   arithmetic over every row rather than a guess, so it wins outright — and it
   wins even when the heading disagrees, which is noted rather than hidden.
2. **The heading.** `QTY Of 5 Units`, `Quantity of 8 Units`, `Qty for 10 boards`.
   Where a sheet has *only* a batch column, the values have to divide cleanly by
   the size the heading names, or the heading is not describing what it claims.
3. **The local model**, asked only when the first two say nothing or disagree.
   Which column is per-board is a reading problem, which is what it is good at.
   It is shown the whole file — every quantity column, values sampled across all
   the rows, and the ratios already computed — and its answer is checked against
   the data before it is used.

The panel says in words how it arrived at the answer, and flags the cases it is
not sure about rather than presenting a guess as a fact. The per-unit figure on
every line is editable, and the order quantity is derived from it — so a
correction survives a change of build size instead of being overwritten by one.

Title rows above the table, notes below it and blank columns in between are all
skipped, and a part number that appears on several lines — one capacitor listed
once per group of designators — is merged into a single order line with the
quantities added together. The panel tells you which strategy was used and how
many rows it skipped, and every quantity is editable before you run anything.

For PDFs the text layer is read directly, with the column geometry preserved so
a table stays a table. A scanned or photographed BOM has no text layer at all;
that case is detected and reported rather than guessed at, and the fix is to
export the BOM as Excel instead.

### Pricing and exporting

Pricing runs in the background and streams back line by line, because a hundred
part numbers is a hundred fan-outs. For each line you get the best offer from
**every** supplier that stocks it — cheapest that can actually ship your
quantity, falling back to cheapest overall when nobody can — with the winner
marked, a running total of the best offer per line, and counts of what was not
found or is short of stock.

### Order minimums, and why the total is what it is

A distributor will not always sell the quantity your BOM asks for. Two separate
constraints can raise it, and both are applied before the line is priced:

- a **minimum order quantity** — ask for 5, the minimum is 10, you buy 10;
- an **order multiple** — ask for 12 where the part ships in steps of 5, you
  buy 15.

**The total is for the quantity your BOM asks for.** A minimum order is a fact
about placing the order, not about what the build costs: a BOM totalled on
minimums answers the question "what if every distributor rounded my order up to
its smallest sellable batch", which can run several times the real requirement
and is useless for costing a board.

So the minimum never enters the total. It is reported instead, and prominently:
the row is **highlighted**, its tag shows the smallest quantity that supplier
will sell, hovering the tag says which constraint applied and what that basket
would cost, and the summary gives the whole-BOM figure at supplier minimums as
its own named number beside the total. Both sheets carry it as
**Supplier Min. Order Qty** and **Cost at Supplier Min.** columns, left blank on
the lines where the minimum is not above what you asked for.

Money is added up in exact decimal arithmetic, with each line rounded to the
currency's own precision before it is added, so the footer always equals the
column of figures above it. A line whose price cannot be converted into the
currency you picked is left out of the total and counted separately rather than
being added in its own currency, which would produce a plausible-looking figure
that is simply wrong.

### When a supplier does not answer

Distributors enforce a per-second query rate. A long BOM is thousands of calls,
so they start refusing them — `403 Account Over Queries Per Second Limit`. That
failure is quiet and expensive: the line still prices from whoever did answer,
so a supplier drops out of the comparison and the cheapest offer can vanish
with it, leaving a more expensive one labelled **best**.

Two things guard against it. Calls to any one supplier are spaced out
(`PROVIDER_MIN_INTERVAL_MS`) and a refusal is retried with a growing backoff
(`PROVIDER_RETRIES`), which clears it in practice. And if a supplier still
fails on a line, that line is recorded as an incomplete comparison: the badge
reads **best of 2** rather than **best**, hovering it names the supplier that
did not answer, and the summary says how many lines are affected. A price the
agent could not check is never presented as one it did.

A part number that a distributor lists but holds none of usually comes back
with no price breaks attached. Those lines are reported as **out of stock**,
not as *not found*: the part number is correct, the supplier and the product
link are shown exactly as they are for a priced line, and the summary counts
them separately. *Not found* is reserved for a part number no supplier lists at
all, which is the one that means "check the BOM for a typo".

Repeated part numbers are merged into one line with their quantities added,
because ordering the same part twice is a real mistake. The merge is never
silent: the file summary reports how many part-number rows were read, how many
unique parts they became, and which part numbers were merged, so every row in
the uploaded file is accounted for.

Exports come in two shapes, both with a totals line:

- **Best price quotes** (Excel) — one row per part, the chosen supplier
  only. This is the sheet a buyer circulates, and it deliberately mirrors the
  layout a distributor's own BOM tool returns, so it drops into an existing
  procurement workflow unchanged:

  | Index | Manufacturer Part Number | Manufacturer Name | Description | Availability | Stock Status | Quantity of *N* Units | Supplier | *Supplier* Part Number 1 | Unit Price 1 | Extended Price *N* units | Supplier Min. Order Qty | Cost at Supplier Min. | Requested Part Number | Product link |

  *N* is the build size you priced at, written into the two headings that depend
  on it. The SKU heading names the distributor when the whole BOM came from
  one, and says `Supplier` when the lines came from several — either way the
  **Supplier** column names the distributor that won each individual line, so a
  BOM split across several is still readable row by row. **Requested Part
  Number** is what your BOM asked for and **Manufacturer Part Number** is what
  the distributor actually listed, so a part that resolved to a near-match is
  visible rather than quietly substituted.

- **All quotes** (Excel) — every supplier that quoted every line, so the choice
  the agent made can be checked rather than taken on trust. The chosen supplier
  is the first row of each line, and the rest follow it:

  | Line | Manufacturer Part Number | Manufacturer | Description | Required qty | Supplier | Supplier min. qty | Stock | Unit price | Total price | Currency | MOQ | Product link |

The Excel files are real `.xlsx` with a frozen header row and autofilter
switched on.

Both are written by `agent/export.py` using nothing but the standard library, so
there is still nothing to `pip install`.

## What the app does

- **Photo identification** -- drop in a picture of a part and a local Ollama
  vision model names it, then it is priced across every supplier automatically.
- **Bill-of-materials pricing** -- upload a BOM as Excel, CSV or PDF and every
  line is priced across every supplier, then exported back to Excel.
- **Parallel fan-out** — every provider is queried at once; one slow or broken
  distributor never blocks or breaks the others, it just reports its own status.
- **Streaming results** — offers render as each distributor replies (SSE), so
  the table fills in progressively rather than after the slowest source.
- **Quantity-aware pricing** — enter the stock you actually need and every unit
  price resolves to the correct volume break, with the line total alongside it.
  Rows where the distributor publishes no break at your quantity are marked
  `Indicative`.
- **Stock vs. requirement** — availability is colour-coded against your required
  quantity, and short rows show exactly how many units are missing.
- **Best-price marking** — per part number, the cheapest offer gets `Best price`
  and the cheapest one that can actually ship your quantity today gets
  `Best in stock`.
- **Currency normalisation** — distributors quote in their own currency; pick a
  display currency and everything converts so the comparison is meaningful. The
  original price stays visible underneath. Rates come from `FX_RATES` in `.env`
  and are approximate — override them if pricing accuracy matters.
- **Component photos** — each row shows the real product image beside the part
  name. Live distributors supply their own product photography, and the offline
  catalogue uses the manufacturer's official photo where one is published
  (Texas Instruments publishes these at a predictable key-free path; run
  `python tools/resolve_images.py` to re-resolve them after editing the
  catalogue). Anything with no photo available falls back to a drawn outline of
  its actual package — DIP, QFN, TO-220, BGA and so on — and a photo URL that
  goes dead at render time degrades to the same outline rather than a broken
  icon.
- **Filter, sort, group, export** — all client-side against data already loaded,
  so none of it costs another API call. Export gives you exactly the rows on
  screen as CSV.
- Light/dark themes, keyboard shortcut `/` to search, shareable URLs, and a
  mobile card layout.

## Project layout

```
app.py                  HTTP server, routing, CSV export, static files
agent/
  config.py             .env loading and settings
  taxonomy.py           hand-written category tree, query matching, reference parts
  catalogue.py          generated distributor category catalogue (988 categories)
  net.py                urllib-based HTTP client with clear error messages
  cache.py              TTL cache for results and OAuth tokens
  fx.py                 currency conversion
  normalize.py          the canonical offer model every provider maps onto
  engine.py             parallel fan-out, merge, ranking, streaming
  vision.py             local Ollama vision model: photo -> component candidates
  bom.py                bill of materials: column detection, pricing, job store
  quantities.py         what a quantity column means, and the build size behind it
  sheets.py             .xlsx and .csv readers (zipfile + ElementTree, no openpyxl)
  pdftext.py            PDF text extraction with column geometry preserved
  export.py             .xlsx writer, plus the text metrics pdftext.py uses
  providers/            one module per distributor
  images.py             generated map of verified product photo URLs
web/                    index.html, styles.css, app.js
tools/resolve_images.py regenerates agent/images.py
tools/fetch_categories.py regenerates agent/catalogue.py from the supplier tree
tools/check_keys.py     validates distributor credentials against the live APIs
```

### Adding another distributor

Create `agent/providers/yoursource.py` with `KEY`, `LABEL`, an `available()`
returning `(bool, reason)` and a `search(query, quantity, display_currency,
limit, category)` that returns `normalize.make_offer(...)` results. `category`
is the resolved taxonomy node id when the query was a category search, or
`None`; ignore it unless your distributor has a native category filter. Add it to `ALL` in
`agent/providers/__init__.py`. The UI, ranking, currency handling, CSV export
and status reporting all pick it up with no further changes.

## Data freshness

**Every page load and every refresh re-queries the suppliers.** Nobody — you or
a customer — is ever shown a stored figure on arrival.

Two things guarantee it:

* The browser searches over the streaming endpoint, which never reads the
  cache. It always goes out to the distributors.
* The first search after a page load additionally sends `fresh=1`, so even the
  fallback JSON path bypasses the cache.

Each result set carries the UTC timestamp of when it was actually retrieved, and
the **Data freshness** tile above the table shows it — `Live · 14:32:05 · 1
source in 624 ms`. If a result ever were served from cache it would read
`Cached` instead, so staleness can never be silent.

The in-memory cache now only serves repeat calls to `/api/search` made without
`fresh=1` — programmatic callers, essentially — and holds for `CACHE_TTL`
seconds (default 60) to protect free-tier quotas on keyed providers. Set
`CACHE_TTL=0` to disable it entirely. The **Refresh** button always bypasses it.

## Accuracy

Stock and pricing come straight from each distributor at query time and change
minute to minute. Volume pricing, regional availability, duties and shipping are
not modelled. Always confirm on the supplier page before committing a build.
