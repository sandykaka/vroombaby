import gc
import os, csv
from urllib.parse import urlparse, parse_qs

from django.core.management.base import BaseCommand

from googlemaps import Client as GoogleMapsClient

import asyncio
from playwright.async_api import async_playwright

import re, hashlib, time
from typing import List, Optional, Dict, Tuple
import pandas as pd
import math
import json
from pathlib import Path
from datetime import timedelta
from django.conf import settings
from difflib import SequenceMatcher
from unidecode import unidecode
import inflect
import spacy
from functools import lru_cache

CACHE_BASE = Path(settings.REVIEWS_CACHE_DIR)

TTL = timedelta(days=7)   # tune as you like
TABS = {"indian","american","chinese","mexican","italian"}
_ETH_MAP = {
    "southasian":"Indian","indiansubcontinent":"Indian",
    "eastasian":"Chinese","hanchinese":"Chinese","chinese":"Chinese",
    "mexican":"Mexican","mexicanamerican":"Mexican",
    "italian":"Italian",
    "angloamerican":"American","northamerican":"American","us":"American","european":"American",
}

BAD_KW = re.compile(
    r"\b(parking|wheelchair|kid[-\s]?friendly|kid[-\s]?friendliness|accessibilit|"
    r"dietary\s+restrictions?|vegetarian\s+(menu|offerings)|gluten[-\s]?free\s+labeled|"
    r"paid\s+parking|parking\s+options)\b", re.I
)

FIELD_HEADERS = [
    "Meal type", "Price per person", "Food:", "Service:",
    "Atmosphere:", "Wait time", "Seating type"
]

TAB_LABELS = {"Indian","American","Chinese","Mexican","Italian"}

class Command(BaseCommand):
    help = "Scrape Google Maps reviews for a place_id, then build dish_mentions for that place."

    def add_arguments(self, parser):
        parser.add_argument("-p", "--place_id", required=True)
        parser.add_argument("--target", type=int, default=40)
        parser.add_argument("--time-budget", type=int, default=12)
        parser.add_argument("--out-dir")
        parser.add_argument("--append", action="store_true")
        parser.add_argument("--fast", action="store_true")

    def handle(self, *args, **options):
        place_id = options["place_id"]
        target = int(options.get("target") or 0)
        time_budget = int(options.get("time_budget") or 0)
        if options.get("fast"):
            target, time_budget = max(target, 24), max(time_budget, 10)
        else:
            target, time_budget = max(target, 150), max(time_budget, 90)

        default_base = Path(getattr(settings, "REVIEWS_CACHE_DIR",
                                    Path(settings.BASE_DIR) / "var" / "reviews"))
        out_dir = Path(options["out_dir"]) if options.get("out_dir") else (default_base / place_id)
        out_dir.mkdir(parents=True, exist_ok=True)

        # 1) Resolve Google Maps canonical URL
        gmaps = GoogleMapsClient(key=settings.GOOGLE_API_KEY)
        try:
            resp = gmaps.place(place_id=place_id, fields=["url"])
            place_url = resp["result"]["url"]
        except Exception as e:
            if "NOT_FOUND" in str(e):
                self.stderr.write("❌ Place ID invalid, aborting.")
                return
            raise

        # 2) Canonicalize: force English and pin to ?cid=… if present
        p = urlparse(place_url)
        q = parse_qs(p.query)
        if "cid" in q:
            cid = q["cid"][0]
            place_url = f"https://www.google.com/maps/place/?cid={cid}&hl=en"
        else:
            sep = "&" if "?" in place_url else "?"
            place_url = f"{place_url}{sep}hl=en"

        # 3) Scrape (async)
        asyncio.run( scrape_reviews(
            place_url=place_url, place_id=place_id, target_reviews=target, time_budget=time_budget, out_dir=out_dir,
            build_mentions_now=True,
            harvest_images_now=True,
            top_k_images=5,
        ))

        # Clear lock if our run created it
        lock = out_dir / ".refresh.lock"
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass

# --- dish normalization (library-based) ---
# pip install spacy unidecode inflect rapidfuzz
# python -m spacy download en_core_web_sm


# load lightweight spaCy pipeline (tokenizer + tagger only)
_nlp = spacy.load("en_core_web_sm", disable=["ner","parser","lemmatizer","textcat"])
_inflect = inflect.engine()
_SMALL = {"and","of","with","on","in","to","for","by","at"}

def _singular(tok: str) -> str:
    s = _inflect.singular_noun(tok)
    return s if isinstance(s, str) and s else tok

@lru_cache(maxsize=4096)
def normalize_dish_key_and_label(text: str) -> Tuple[str, str]:
    """
    Returns (key, display_label).
    key: lowercase, no leading DET, unified connectors, punctuation stripped, singularized.
    label: Title Case (keeps small words lowercase) for UI.
    """
    if not text:
        return "", ""
    t = unidecode(str(text)).strip()
    if not t:
        return "", ""

    doc = _get_nlp()(t)

    norm_tokens = []
    for i, tok in enumerate(doc):
        if i == 0 and tok.pos_ == "DET":  # drop leading 'the/a/an' via POS
            continue
        if tok.is_space or tok.is_punct:
            continue

        s = tok.text.lower()
        if s in {"&", "n", "n."}:
            s = "and"

        s = re.sub(r"[’'`]", "", s)          # apostrophes
        s = re.sub(r"[-_/]", " ", s)         # separators -> space
        s = re.sub(r"[^a-z0-9\s]", " ", s)   # other punct -> space
        s = re.sub(r"\s+", " ", s).strip()
        if not s:
            continue

        parts = [_singular(p) for p in s.split()]
        norm_tokens.extend(parts)

    key = " ".join(norm_tokens).strip()
    if not key:
        return "", ""

    words = key.split()
    label = " ".join(w if (i and w in _SMALL) else w.capitalize()
                     for i, w in enumerate(words))
    return key, label

# Add this helper near the top of scrape_reviews.py
def _norm_text(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

_nlp = None

def _get_nlp():
    global _nlp
    if _nlp is None:
        import spacy
        _nlp = spacy.load("en_core_web_sm", disable=["ner","parser","lemmatizer","textcat"])
    return _nlp

def _read_seed_reviews(out_dir: Path):
    """Return (seed_reviews, seen_ids, seen_text_norm) from reviews.json if present."""
    src = out_dir / "reviews.json"
    if not src.exists():
        return [], set(), set()
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        return [], set(), set()

    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    for r in data:
        rid = (r.get("id") or "").strip()
        txt = _norm_text(r.get("text") or "")
        if rid:
            seen_ids.add(rid)
        elif txt:
            seen_text.add(txt)
    return data, seen_ids, seen_text


def _aggregate_now(out_dir: Path, label: str = ""):  # === NEW ===
    """
    Rebuild authors.csv and dish_mentions.csv from current reviews.json.
    Shield any exception so scraping can continue.
    """
    try:
        reviews_json = str(out_dir / "reviews.json")
        authors_csv  = str(out_dir / "authors.csv")

        # authors (incremental if your helper supports it)
        authors_csv_path = write_or_update_authors_csv(reviews_json, authors_csv)

        # choose lexicon
        prefer   = out_dir / "dish_lexicon.csv"
        fallback = Path(settings.BASE_DIR) / "data" / "dish_lexicon.csv"
        lexicon_csv_path = str(prefer if prefer.exists() else fallback)

        # aggregate to dish_mentions.csv
        build_dish_mentions(
            reviews_json=reviews_json,
            authors_csv=authors_csv_path,
            lexicon_csv=lexicon_csv_path,
            out_csv=str(out_dir / "dish_mentions.csv"),
            save_raw_csv=str(out_dir / "dish_mentions_raw.csv"),
            mode="both",
            limit_per_ethnicity=5,                # writes dish_mentions_top5.csv
            out_csv_topk=str(out_dir / "dish_mentions_top5.csv"),
        )
        print(f"🟢 aggregated ({label}) → {out_dir/'dish_mentions.csv'}")
    except Exception as e:
        print(f"⚠️ aggregate failed ({label}): {e}")

async def scrape_reviews(
        place_url: str,
        place_id: str,
        target_reviews: int,
        time_budget: Optional[float],
        out_dir: Path,
        *,
        build_mentions_now: bool = True,
        harvest_images_now: bool = True,
        top_k_images: int = 5,
):
    # ---- locks ----
    lock = out_dir / ".refresh.lock"
    try: lock.write_text(str(os.getpid()), encoding="utf-8")
    except Exception: pass

    seed_reviews, seen_ids, seen_text = _read_seed_reviews(out_dir)
    seed_seen_count = len(seed_reviews)
    total_reviews = int(target_reviews or 0)

    # pre-warm tagger so aggregation later is instant
    try:
        _get_nlp()("warm up")
    except Exception:
        pass

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            chromium_sandbox=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-crash-reporter",
                # no "--single-process"
            ],
        )

        context = await browser.new_context(locale="en-US")
        page = await context.new_page()
        page.set_default_timeout(15_000)

        # request blocking (reviews phase only)
        BLOCK_TYPES = {"image", "font", "stylesheet", "media"}
        SNIPPETS = ("/maps/vt", "lh3.googleusercontent.com", "ggpht.com",
                    "fonts.gstatic.com", ".woff", ".woff2", ".ttf",
                    "/gen_204", "/collect")
        async def _route_filter(route):
            req = route.request
            if (req.resource_type in BLOCK_TYPES) or any(s in req.url for s in SNIPPETS):
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", _route_filter)

        deadline = (time.perf_counter() + time_budget) if time_budget else None

        try:
            # ---------- scrape reviews ----------
            await page.add_style_tag(content="""
              *,*::before,*::after{animation:none!important;transition:none!important}
              html{scroll-behavior:auto!important}
            """)
            await page.goto(place_url, wait_until="domcontentloaded")

            handle = await page.evaluate_handle(
                """() => {
                    const card = document.querySelector("div[data-review-id]");
                    if (!card) return document.scrollingElement;
                    let c = card.closest('[role=region]');
                    if (!c) c = document.querySelector('div.section-scrollbox');
                    return c || document.scrollingElement;
                }"""
            )
            scroll_el = handle.as_element()
            locator = page.locator('div[data-review-id]')

            await locator.first.wait_for(state="visible", timeout=10_000)
            for _ in range(3):
                await scroll_el.evaluate("el => el.scrollBy(0, el.clientHeight * 0.25)")
                await page.wait_for_timeout(300)

            more_reviews = page.locator('text=/More reviews/').first
            if await more_reviews.count():
                await more_reviews.scroll_into_view_if_needed()
                await more_reviews.click()
                await page.wait_for_timeout(600)

            try:
                await page.get_by_role("button", name="Sort reviews").click()
                menu = page.get_by_role("menu")
                await menu.wait_for(state="visible", timeout=5_000)
                await menu.get_by_role("menuitemradio", name="Highest rating").click()
                await page.wait_for_timeout(250)
            except Exception:
                pass

            if seed_seen_count:
                curr = await locator.count()
                ff_target = min(total_reviews, max(curr, seed_seen_count + 50))
                stagnant = 0; max_steps = 60
                while curr < ff_target and max_steps > 0:
                    await scroll_el.evaluate("el => el.scrollBy(0, el.clientHeight)")
                    await page.wait_for_timeout(180)
                    nxt = await locator.count()
                    if nxt <= curr:
                        stagnant += 1
                        if stagnant >= 3:
                            break
                    else:
                        stagnant = 0; curr = nxt
                    max_steps -= 1

            reviews = list(seed_reviews)
            batch_size = 20
            prev_count = 0
            while True:
                curr_count = await locator.count()
                if curr_count <= prev_count:
                    await scroll_el.evaluate("el => el.scrollBy(0, el.clientHeight * 0.6)")
                    await page.wait_for_timeout(300)
                    curr_count = await locator.count()
                    if curr_count <= prev_count:
                        break

                end = min(prev_count + batch_size, curr_count)

                await page.evaluate(
                    """([start,end]) => {
                        const cards = Array.from(document.querySelectorAll('div[data-review-id]')).slice(start,end);
                        for (const el of cards) {
                            const btn = el.querySelector('button[aria-label="See more"]');
                            if (btn) btn.click();
                        }
                    }""",
                    [prev_count, end]
                )

                batch = await page.evaluate(
                    """([start,end]) => {
                        const out = [];
                        const cards = Array.from(document.querySelectorAll('div[data-review-id]')).slice(start,end);
                        for (const el of cards) {
                            const id = el.getAttribute('data-review-id') || "";
                            let author = "";
                            const avatar = el.querySelector('button[aria-label^="Photo of "]');
                            if (avatar) author = (avatar.getAttribute('aria-label') || "").replace(/^Photo of\\s+/i, "").trim();
                            if (!author) author = (el.getAttribute('aria-label') || "").trim();
                            if (!author) {
                                const prof = el.querySelector('button[jsaction*="reviewerLink"] div');
                                if (prof) author = (prof.textContent || "").split("\\n")[0].trim();
                            }
                            const txtEl = el.querySelector('[lang]');
                            const text = txtEl ? txtEl.innerText.trim() : "";
                            out.push({ id, author, text });
                        }
                        return out;
                    }""",
                    [prev_count, end]
                )

                for entry in batch:
                    rid  = (entry.get("id") or "").strip()
                    text = entry.get("text") or ""
                    key_text = _norm_text(text)
                    if rid:
                        if rid in seen_ids: continue
                        seen_ids.add(rid)
                    else:
                        if key_text in seen_text: continue
                        if key_text: seen_text.add(key_text)
                    entry["author"] = entry.get("author") or ""
                    reviews.append(entry)

                (out_dir / "reviews.json").write_text(json.dumps(reviews, indent=2), encoding="utf-8")

                prev_count = end
                if len(reviews) >= total_reviews: break
                if deadline and time.perf_counter() >= deadline: break

        finally:
            # turn OFF request blocking; we'll reuse context for images
            try:
                await context.unroute("**/*", _route_filter)
            except Exception:
                try: await context.unroute("**/*")
                except Exception: pass
            try:
                await page.close()
            except Exception:
                pass

        # ---------- FAST aggregate (optional, while browser still open) ----------
        if build_mentions_now:
            try:
                _aggregate_now(out_dir, label="fast")
            except Exception as e:
                print(f"⚠️ aggregate failed (fast): {e}")

        # ---------- harvest images using SAME context ----------
        if harvest_images_now:
            try:
                top_dishes = _top_dishes_for_images(out_dir, top_k=2, dedupe=True)
            except Exception:
                top_dishes = []

            if top_dishes:
                # skip already-downloaded filenames
                have = _existing_image_stems(out_dir)
                top_dishes = [d for d in top_dishes if d.lower() not in have]

            if top_dishes:
                page2 = await context.new_page()
                try:
                    await page2.goto(place_url, wait_until="domcontentloaded")
                    await _harvest_menu_images_on_page(page2, top_dishes, out_dir, max_scrolls=60)
                    print(f"🖼️ harvested images for {len(top_dishes)} dishes (FAST top{top_k_images}).")
                except Exception as e:
                    print(f"⚠️ menu image harvest failed: {e}")
                finally:
                    try: await page2.close()
                    except Exception: pass

        # ---------- close once ----------
        try: await context.close()
        except Exception: pass
        try: await browser.close()
        except Exception: pass

    # Aggregate outside the browser for a final, consistent output
    if not build_mentions_now:
        _aggregate_now(out_dir, label="post-scrape")
    for name in (".refresh.lock", ".enqueue.lock"):
        try: (out_dir / name).unlink(missing_ok=True)
        except Exception: pass
    gc.collect()


# ---------- keys & mapping ----------
def author_key_from_name(name: str) -> str:
    norm = (name or "").strip().lower()
    norm = re.sub(r"\s+", " ", norm)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()

_ARTICLES = {"the", "a", "an"}

def normalize_dish(s: str) -> str:
    """
    Normalizes a dish string for stable matching:
    - trim, lowercase
    - unify & -> and
    - strip apostrophes/punctuation noise
    - collapse whitespace
    - (display form) Title-Case except small words like 'and'
    """
    if not s:
        return ""
    t = s.strip().lower()

    # unify connectors and punctuation
    t = t.replace("&", " and ")
    t = re.sub(r"[’'`]", "", t)           # drop apostrophes
    t = re.sub(r"[-_/]", " ", t)          # normalize separators to space
    t = re.sub(r"[^a-z0-9\s]", " ", t)    # strip other punctuation
    t = re.sub(r"\s+", " ", t).strip()

    # we keep leading articles here so the raw text is preserved;
    # de-duplication happens on dish_key() during aggregation.
    if not t:
        return ""

    # Pretty label (Title Case, keep 'and' lowercase)
    words = t.split()
    out = []
    for w in words:
        if w in {"and", "of", "with"}:
            out.append(w)
        else:
            out.append(w.capitalize())
    return " ".join(out)


def map_group_to_ui(group_chain: Optional[str]) -> str:
    if not group_chain: return "Unknown"
    toks = [t.strip().lower() for t in group_chain.split(",") if t.strip()]
    for t in toks:
        if t in _ETH_MAP: return _ETH_MAP[t]
    return toks[0].capitalize() if toks else "Unknown"

# ---------- “Recommended dishes …” extractor ----------
_RE_RECOMMENDED = re.compile(r"(?:^|\n)\s*recommended\s+dish(?:es)?\s*[:\-]?\s*", re.I)
_RE_SECTION_STOP = re.compile(r"(?:\n{2,}|^|\n)(?:Food:|Service:|Atmosphere:|Price per person|Wait time|Seating type|Photos|Like|Share)\b", re.I)
_SPLIT_DISHES = re.compile(r"\s*(?:,|·|•|/|\band\b|\&)\s*", re.I)

def extract_recommended_dishes(text: str) -> List[str]:
    if not text: return []
    m = _RE_RECOMMENDED.search(text)
    if not m: return []
    chunk = text[m.end():]
    stop = _RE_SECTION_STOP.search(chunk)
    if stop: chunk = chunk[:stop.start()]
    chunk = chunk.strip()[:300]
    parts = [p.strip() for p in _SPLIT_DISHES.split(chunk) if p.strip()]
    out, seen = [], set()
    for p in parts:
        p = re.sub(r'^[\-\u2022\u2023\u25E6\u2043\u2219"\']+\s*', "", p).strip()
        if len(p) < 2: continue
        p = re.sub(r"\s+", " ", p).strip()
        if not re.search(r"[A-Z]{2,}", p): p = p.title()
        key = p.lower()
        if key not in seen:
            seen.add(key); out.append(p)
    return out

# ---------- LEXICON support ----------
def load_lexicon(lexicon_csv: Optional[str]) -> Dict[str, List[str]]:
    """
    CSV columns required: dish,synonym
    Returns {canonical_dish: [synonyms...]}.
    Falls back to a tiny built-in sample if file missing.
    """
    if lexicon_csv and Path(lexicon_csv).exists():
        df = pd.read_csv(lexicon_csv, dtype=str).fillna("")
        df = df[(df["dish"]!="") & (df["synonym"]!="")]
        lex: Dict[str, List[str]] = {}
        for _, r in df.iterrows():
            lex.setdefault(r["dish"].strip(), []).append(r["synonym"].strip())
        return lex
    # minimal fallback so you’re never blocked
    return {
        "Fried Chicken": ["fried chicken", "buttermilk fried chicken"],
        "Tavern Burger": ["tavern burger", "the tavern burger", "burger and fries"],
        "Deviled Eggs": ["deviled eggs", "southern deviled eggs"],
        "Mac And Cheese": ["mac & cheese", "mac n cheese", "mac and cheese"],
        "Dumplings": ["dumpling", "dumplings"],
        "Fried Rice": ["fried rice"],
        "Pasta": ["pasta"],
    }

def _phrase_to_regex(phrase: str) -> str:
    # allow space-or-hyphen between words, word boundaries around
    words = [re.escape(w) for w in phrase.split()]
    body  = r"[\s\-]+".join(words)
    return rf"(?<!\w){body}(?!\w)"

def build_dish_mentions(
        reviews_json: str,
        authors_csv: Optional[str] = None,
        out_csv: str = "dish_mentions.csv",
        save_raw_csv: Optional[str] = None,
        lexicon_csv: Optional[str] = "dish_lexicon.csv",
        mode: str = "both",   # "recommended" | "lexicon" | "both"
        limit_per_ethnicity: Optional[int] = None,      # <-- NEW
        out_csv_topk: Optional[str] = "dish_mentions_top5.csv",  # <-- NEW
) -> pd.DataFrame:
    t0 = time.time()

    # ------------ load reviews ------------
    data = json.loads(Path(reviews_json).read_text(encoding="utf-8"))
    reviews_df = pd.DataFrame([
        {"author": d.get("author","").strip(), "text": (d.get("text") or "").strip()}
        for d in data if (d.get("text") or "").strip()
    ])
    if reviews_df.empty:
        Path(out_csv).write_text("", encoding="utf-8")
        if save_raw_csv:
            Path(save_raw_csv).write_text("", encoding="utf-8")
        return reviews_df

    reviews_df["author_key"] = reviews_df["author"].apply(author_key_from_name)

    # ------------ join authors.csv (author_key, group) ------------
    # ensure the column exists so merge can suffix if needed
    reviews_df["group"] = pd.NA

    if authors_csv and Path(authors_csv).exists():
        a = pd.read_csv(authors_csv, dtype=str).rename(columns=lambda c: c.strip())
        keep = [c for c in ("author_key","group") if c in a.columns]
        if keep:
            a = a[keep].drop_duplicates()
            reviews_df = reviews_df.merge(a, on="author_key", how="left")

    # unify merge suffixes: group_x/group_y -> group
    if "group_x" in reviews_df.columns or "group_y" in reviews_df.columns:
        gx = reviews_df.get("group_x")
        gy = reviews_df.get("group_y")
        if gx is not None:
            gx = gx.astype("string").replace({"": pd.NA, "None": pd.NA})
        if gy is not None:
            gy = gy.astype("string").replace({"": pd.NA, "None": pd.NA})

        if gx is None:
            reviews_df["group"] = gy
        elif gy is None:
            reviews_df["group"] = gx
        else:
            reviews_df["group"] = gx.combine_first(gy)

        reviews_df = reviews_df.drop(columns=[c for c in ("group_x","group_y") if c in reviews_df.columns])

    if "group" not in reviews_df.columns:
        reviews_df["group"] = pd.Series([pd.NA]*len(reviews_df), dtype="string")

    # ------------ map to UI + tab (once) ------------
    g = reviews_df["group"].astype("string").str.strip()
    g = g.mask(g.str.lower().isin({"", "none", "nan"}))
    reviews_df["ethnicity_ui"] = g.apply(lambda x: map_group_to_ui(x) if isinstance(x, str) else None)
    reviews_df["tab"] = g.apply(lambda x: map_group_to_tab(x) if isinstance(x, str) else None)

    print("build_dish_mentions(): columns ->", list(reviews_df.columns))
    print("head ->", reviews_df.head(2))

    # ------------ lexicon index ------------
    use_lex = mode in ("lexicon","both")
    idx = build_lexicon_index(load_lexicon(lexicon_csv)) if use_lex else []

    # ------------ collect dish mentions ------------
    rows = []
    for _, row in reviews_df.iterrows():
        rtext = row["text"]

        dishes_rec = extract_recommended_dishes(rtext) if mode in ("recommended","both") else []
        dishes_lex = extract_with_lexicon(rtext, idx) if use_lex else []

        # build a set of normalized KEYS that came from the "recommended" extractor
        rec_key_set = set()
        for d in dishes_rec:
            k, _ = normalize_dish_key_and_label(d)
            if k:
                rec_key_set.add(k)

        # union by canonical key, keep a pretty label
        seen_keys = set()
        for d in (dishes_rec + dishes_lex):
            k, label = normalize_dish_key_and_label(d)
            if not k or k in seen_keys:
                continue
            seen_keys.add(k)

            rows.append({
                "author_key": row["author_key"],
                "author": row["author"],
                "group": row["group"],
                "tab": row["tab"],
                "ethnicity_ui": row["ethnicity_ui"],
                "dish_key": k,          # <-- canonical for grouping
                "dish": label,          # <-- pretty label for UI
                "text": rtext,
                "source": ("recommended" if k in rec_key_set else "lexicon"),
            })
    raw = pd.DataFrame(rows)

    # optional raw dump
    if save_raw_csv:
        raw.to_csv(save_raw_csv, index=False)

    # if nothing, still produce the (empty) out_csv
    if raw.empty:
        Path(out_csv).write_text("", encoding="utf-8")
        print(f"ℹ️ No dish mentions found (mode={mode}).")
        return raw

    # ------------ aggregate for UI ------------
    raw = raw[raw["tab"].isin(TAB_LABELS)]
    if raw.empty:
        Path(out_csv).write_text("", encoding="utf-8")
        print(f"ℹ️ No dish mentions after tab filter.")
        return raw

    # pick a representative display per (tab, dish_key)
    rep = (
        raw.groupby(["tab","dish_key"])["dish"]
        .agg(lambda s: s.value_counts().index[0])
        .reset_index()
        .rename(columns={"dish": "dish_display"})
    )

    agg = (
        raw.groupby(["tab","dish_key"], dropna=False)
        .agg(
            mentions=("dish","count"),
            unique_authors=("author_key", pd.Series.nunique),
            from_recommended=("source", lambda s: int((s == "recommended").any())),
        )
        .reset_index()
        .merge(rep, on=["tab","dish_key"], how="left")
        .rename(columns={"tab": "ethnicity_ui"})
    )

    # finalize columns for CSVs: use display label as 'dish'
    agg = (agg
           .rename(columns={"dish_display": "dish"})
           .drop(columns=["dish_key"])
           .sort_values(["ethnicity_ui","mentions","unique_authors","dish"],
                        ascending=[True, False, False, True]))

    agg.to_csv(out_csv, index=False)

    # optional top-k file remains consistent
    if "limit_per_ethnicity" in locals() and limit_per_ethnicity is not None and out_csv_topk:
        tmp = agg.copy()
        tmp["__rank"] = tmp.groupby("ethnicity_ui").cumcount()
        topk_df = tmp[tmp["__rank"] < int(limit_per_ethnicity)].drop(columns="__rank")
        topk_df.to_csv(out_csv_topk, index=False)
        print(f"✅ dish_mentions (TOP{limit_per_ethnicity}) → {out_csv_topk}  ({len(topk_df)} rows)")

    print(f"✅ dish_mentions (FULL) → {out_csv}  ({len(agg)} rows; mode={mode}; {time.time()-t0:.2f}s)")
    return agg



def extract_with_lexicon(text: str, idx: List[Tuple[str, re.Pattern]]) -> List[str]:
    if not text: return []
    hits = []
    for dish, pat in idx:
        if pat.search(text):
            hits.append(dish)
    return hits


def build_lexicon_index(lex: Dict[str, List[str]]) -> List[Tuple[str, re.Pattern]]:
    """
    Returns list of (canonical_dish, compiled_pattern) covering both
    canonical name and all synonyms. Case-insensitive.
    """
    idx: List[Tuple[str, re.Pattern]] = []
    for dish, syns in lex.items():
        forms = [dish] + syns
        alts  = "|".join(_phrase_to_regex(f) for f in forms if f.strip())
        if not alts: continue
        pat = re.compile(alts, re.I)
        idx.append((dish, pat))
    return idx


def _safe_map_group_to_ui(v):
    # treat empty/None/"None"/NaN/"nan" as missing
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s or s.lower() in {"none", "nan"}:
            return None
        return map_group_to_ui(s)
    # anything else (e.g., pandas NA)
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    return map_group_to_ui(str(v).strip())

def extract_from_recommended(text: str) -> list[str]:
    if not text:
        return []
    hdr = "|".join(map(re.escape, FIELD_HEADERS))
    pat = re.compile(
        rf"(?:Recommended|Popular)\s+dishes\s*[:\n]\s*(.+?)(?=\n(?:{hdr})\b|\Z)",
        re.I | re.S
    )
    m = pat.search(text)
    if not m:
        return []
    return split_candidates(m.group(1))


def split_candidates(block: str) -> list[str]:
    parts = re.split(r"(?:,|\band\b|/|•|·|\u2022|\n)", block, flags=re.I)
    return [p.strip() for p in parts if p and len(p.strip()) >= 3]

def map_group_to_tab(chain):
    if not isinstance(chain, str) or not chain.strip():
        return None
    toks = {t.strip().lower() for t in chain.split(",") if t}

    if "southasian" in toks or "indian" in toks:
        return "Indian"
    if "eastasian" in toks or "chinese" in toks:
        return "Chinese"
    if any(t in toks for t in ("hispanic", "latino", "mexican")):
        return "Mexican"
    if "italian" in toks:
        return "Italian"

    # Map African/European buckets to American
    if any(t in toks for t in ("greatereuropean", "european", "greaterafrican", "african")):
        return "American"

    # Skip unknowns and anything we don't recognize
    if "unknown" in toks:
        return None
    return None

def _title_name(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    parts = re.split(r"(\s+)", s.lower())
    return "".join(p.capitalize() if p.strip() else p for p in parts)

def _split_first_last(author_norm: str):
    parts = [p for p in (author_norm or "").split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]

def write_or_update_authors_csv(reviews_json_path: str, authors_csv_path: str) -> str:
    data = json.loads(Path(reviews_json_path).read_text(encoding="utf-8"))

    # count reviews per author_key
    counts = {}
    for r in data:
        name = (r.get("author") or "").strip()
        if not name:
            continue
        ak = author_key_from_name(name)
        counts[ak] = counts.get(ak, 0) + 1

    # build new rows
    rows = []
    for ak, n in counts.items():
        raw = next(((r.get("author") or "").strip()
                    for r in data
                    if author_key_from_name((r.get("author") or "").strip()) == ak), "")
        author_norm = _title_name(raw)
        first, last = _split_first_last(author_norm)
        rows.append({
            "author_key": ak,
            "author_norm": author_norm,
            "first": first,
            "last": last,
            "group": "",   # blank until we enrich
            "prob": "",
            "lens": "unknown",
            "review_count_by_author": str(n),
            "author_display": author_norm or raw,
        })

    new = pd.DataFrame(rows, dtype=str).fillna("")
    p = Path(authors_csv_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    if p.exists():
        existing = pd.read_csv(p, dtype=str).fillna("")
        combined = pd.concat([existing, new], ignore_index=True, sort=False)
        combined = combined.drop_duplicates(subset=["author_key"], keep="first")

        # only blank rows need enrichment
        mask = combined["group"].astype(str).str.strip().eq("") | combined["prob"].astype(str).str.strip().eq("")

        if mask.any():
            to_enrich = combined.loc[mask].copy()
            to_enrich = enrich_groups_with_ethnicolr(to_enrich, prob_threshold=0.7)
            # write back enriched cols only
            for col in ["group", "prob", "lens"]:
                combined.loc[mask, col] = to_enrich[col].values

        combined.to_csv(p, index=False)
        print(f"✅ Updated authors.csv → {p}  ({len(combined)} authors)")
    else:
        # ✅ enrich on first create too
        new = enrich_groups_with_ethnicolr(new, prob_threshold=0.7)
        new.to_csv(p, index=False)
        print(f"✅ Created authors.csv → {p}  ({len(new)} authors)")

    return str(p)

def enrich_groups_with_ethnicolr(df: pd.DataFrame, prob_threshold: float = 0.7) -> pd.DataFrame:
    """
    Fill df['group'] for rows where it's blank using Ethnicolr.
    Does NOT overwrite existing non-blank groups.
    Robust to Ethnicolr schema/version differences.
    """
    try:
        import ethnicolr  # pip install ethnicolr
    except Exception:
        print("[authors] ethnicolr not installed; skipping auto group fill")
        return df

    df = df.copy()
    if "group" not in df.columns:
        df["group"] = ""

    # Only rows that need a label and have at least a first or last name
    need = df["group"].fillna("").eq("")
    if "first" not in df.columns or "last" not in df.columns:
        print("[authors] missing first/last columns; cannot run ethnicolr")
        return df
    sub = df.loc[need, ["first", "last"]].fillna("")
    sub = sub[(sub["first"] != "") | (sub["last"] != "")]
    if sub.empty:
        return df

    # --- Run Ethnicolr (Wiki model) ---
    try:
        pred = ethnicolr.pred_wiki_name(sub.rename(columns={"first": "first", "last": "last"}),
                                        lname_col="last", fname_col="first")
    except TypeError:
        pred = ethnicolr.pred_wiki_name(sub.rename(columns={"first": "first", "last": "last"}),
                                        "last", "first")

    # Build a case-insensitive column lookup
    cols_lc = {c.lower(): c for c in pred.columns}

    # --- Detect label column across versions (case-insensitive) ---
    label_col = None
    for cand in ["race", "ethnicity", "pred", "race_ethnicity"]:
        if cand in cols_lc:
            label_col = cols_lc[cand]
            break
    if label_col is None:
        print("[authors] ethnicolr returned unexpected schema; skipping auto fill")
        return df

    # --- Detect probability column (single) or compute from distributed ---
    prob_col = None
    for cand in ["prob", "probability", "race_prob", "ethnicity_prob"]:
        if cand in cols_lc:
            prob_col = cols_lc[cand]
            break

    if prob_col is None:
        # distributed probs: any columns starting with prob_ or p_
        prob_cols = [c for c in pred.columns
                     if c.lower().startswith("prob_") or c.lower().startswith("p_")]
        if prob_cols:
            # coerce to numeric, compute row-wise max
            pred["_prob_max"] = pd.to_numeric(pred[prob_cols], errors="coerce").max(axis=1)
            # pick label with max prob if we didn't get a label_col earlier
            argmax = pd.to_numeric(pred[prob_cols], errors="coerce").idxmax(axis=1)
            pred["_label_from_probs"] = argmax.str.replace(r"^(prob_|p_)", "", regex=True)
            # prefer explicit label if present, otherwise use derived
            if label_col is None:
                label_col = "_label_from_probs"
            prob_col = "_prob_max"

    # Build series; align indices with `sub`
    lab_series = pred[label_col].astype(str)
    if not lab_series.index.equals(sub.index):
        lab_series.index = sub.index

    if prob_col and prob_col in pred.columns:
        p_series = pd.to_numeric(pred[prob_col], errors="coerce")
        if not p_series.index.equals(sub.index):
            p_series.index = sub.index
    else:
        p_series = pd.Series(1.0, index=sub.index, dtype=float)

    mapped = lab_series.str.lower().map(to_chain)

    # --- Apply back to df for rows over threshold and still blank ---
    to_fill_idx = mapped.index[(mapped != "") & (p_series >= prob_threshold)]
    if len(to_fill_idx):
        df.loc[to_fill_idx, "group"] = mapped.loc[to_fill_idx].values
        if "prob" not in df.columns:
            df["prob"] = ""
        df.loc[to_fill_idx, "prob"] = p_series.loc[to_fill_idx].round(3).astype(str).values

    print(f"[authors] ethnicolr filled groups for {len(to_fill_idx)} authors (thr={prob_threshold})")
    return df


# --- Map Ethnicolr label -> your taxonomy chain ---
def to_chain(label: str) -> str:
        lbl = (label or "").lower()

        # Handle chain-like labels Ethnicolr emits (examples seen in your logs)
        if "indiansubcontinent" in lbl or "indian" in lbl or "southasian" in lbl:
            return "SouthAsian,IndianSubContinent"
        if "eastasian" in lbl or "chinese" in lbl or "japanese" in lbl or "korean" in lbl:
            return "Asian,GreaterEastAsian,EastAsian"
        if "italian" in lbl:
            return "GreaterEuropean,WestEuropean,Italian"
        if "hispanic" in lbl or "latino" in lbl:
            return "Mexican"  # coarse bucket used by your app
        if "greatereuropean" in lbl or "easteuropean" in lbl or "westeuropean" in lbl or lbl == "white":
            return "GreaterEuropean"
        if "greaterafrican" in lbl or "african" in lbl or lbl == "black":
            return "GreaterAfrican"

        # generic fallbacks
        if lbl == "asian":
            return "Asian,GreaterEastAsian,EastAsian"
        return ""  # unknown/low confidence → leave blank

def _norm(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _label_score(dish: str, label: str) -> float:
    """Blend token overlap + fuzzy ratio; no hard-coded synonyms."""
    if not dish or not label:
        return 0.0
    d, l = _norm(dish), _norm(label)

    if d == l:          # exact
        return 1.0
    if d in l or l in d:  # containment
        return 0.92

    dt, lt = set(d.split()), set(l.split())
    jacc = (len(dt & lt) / len(dt | lt)) if dt and lt else 0.0
    fuzz = SequenceMatcher(None, d, l).ratio()
    return min(1.0, 0.65 * jacc + 0.45 * fuzz)

def _top_dishes_for_images(out_dir: Path, top_k: int = 5, dedupe: bool = True) -> list[str]:
    csv_path = out_dir / "dish_mentions.csv"
    if not csv_path.exists():
        return []
    df = pd.read_csv(csv_path, dtype=str).rename(columns=lambda c: c.strip())
    low = {c.lower(): c for c in df.columns}
    req = ["ethnicity_ui","dish","mentions","unique_authors"]
    if not all(k in low for k in req):
        return []
    ecol, dcol = low["ethnicity_ui"], low["dish"]
    mcol, ucol = low["mentions"], low["unique_authors"]
    df[mcol] = pd.to_numeric(df[mcol], errors="coerce").fillna(0).astype(int)
    df[ucol] = pd.to_numeric(df[ucol], errors="coerce").fillna(0).astype(int)
    df = df.sort_values([ecol, mcol, ucol, dcol], ascending=[True, False, False, True])
    df["__rank"] = df.groupby(ecol).cumcount()
    df_top = df[df["__rank"] < int(top_k)]
    dishes = df_top[dcol].astype(str).str.strip().tolist()
    if not dedupe:
        return [d for d in dishes if d]
    seen=set(); out=[]
    for d in dishes:
        k=d.lower()
        if d and k not in seen:
            seen.add(k); out.append(d)
    return out

def _existing_image_stems(out_dir: Path) -> set[str]:
    imgs_dir = out_dir / "menu_images"
    stems = set()
    if imgs_dir.exists():
        for ext in ("*.jpg","*.jpeg","*.png","*.webp"):
            for p in imgs_dir.glob(ext):
                stems.add(p.stem.casefold())
    return stems

async def _harvest_menu_images_on_page(
        page,                     # <-- Playwright Page that is already on place_url
        top_dishes: list[str],
        out_dir: Path,
        max_scrolls: int = 60,
):
    """
    Re-uses an existing Playwright page (images allowed) to:
      - switch to the Menu tab,
      - scroll through the highlights carousel/grid,
      - try to match aria-labels to each dish name,
      - save the first matching image per dish to dish_images.json.
    """
    # 1) Go to "Menu" tab with resilient queries
    # (keep your robust role/text matching logic)
    menu_tab = page.locator('role=tab[name=/menu/i]').first
    if await menu_tab.count():
        await menu_tab.click()
        await page.wait_for_timeout(350)
    else:
        # fallback: text match
        mt = page.locator("text=/^Menu$/i").first
        if await mt.count():
            await mt.click()
            await page.wait_for_timeout(350)

    # 2) Now walk through highlight buttons/images and match aria-labels
    #    Keep your current robust matching (_label_score, SequenceMatcher, etc.)
    found: dict[str, dict] = {}
    seen = set()

    # A resilient query for items that have an image and an aria-label
    items = page.locator('button[aria-label] img, img[alt][crossorigin]').locator("..")  # go back to button if needed

    scrolls = 0
    while scrolls < max_scrolls and len(found) < len(top_dishes):
        count = await items.count()
        for i in range(count):
            btn = items.nth(i)
            # normalize label
            label = (await btn.get_attribute("aria-label")) or ""
            if not label:
                # try the img alt
                img = btn.locator("img").first
                if await img.count():
                    label = (await img.get_attribute("alt")) or ""

            if not label:
                continue

            # best-effort label matching (your existing _label_score)
            for dish in top_dishes:
                if dish in found:  # already found one image for this dish
                    continue
                score = _label_score(label, dish)
                if score >= 0.66:  # tune threshold as you like
                    img = btn.locator("img").first
                    if await img.count():
                        src = await img.get_attribute("src")
                        if src and src not in seen:
                            seen.add(src)
                            found[dish] = {"image_url": src, "caption": label}
        if len(found) >= len(top_dishes):
            break

        # try to reveal more items (scroll the container and page)
        await page.mouse.wheel(0, 1200)
        await page.wait_for_timeout(250)
        scrolls += 1

    # 3) Persist to dish_images.json (merge with any existing)
    path = out_dir / "dish_images.json"
    try:
        existing = json.loads(path.read_text("utf-8"))
    except Exception:
        existing = {}

    changed = False
    for dish, val in found.items():
        if dish not in existing:
            existing[dish] = val
            changed = True

    if changed:
        path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
