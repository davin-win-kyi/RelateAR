#!/usr/bin/env python3
"""
select_best_product_image.py

Pipeline:
1) Calls extract_url_info.extract_with_gpt5(url) -> {"company_name": str, "product_name": [str, ...]}
2) Calls generic_web_scraper.main(url, company_name) -> expects a dict with keys like:
      {
        "image_urls": [str, ...],
        "dimensions": {
            "length": "...", "width": "...", "height": "..."
        }
        # (Your scraper can return additional fields; they'll be passed through.)
      }
3) Uses GPT-5 twice:
    a) to expand product name aliases (e.g., "couch" -> "sofa", etc.)
    b) to rank the images and choose the single best image where the main object is most visible
       and minimally occluded (measurement overlays are allowed).

Output (printed as JSON):
{
  "url": "<input URL>",
  "company_name": "<company>",
  "product_names": ["sectional", "couch", "sofa", ...],   # enriched aliases
  "dimensions": {...},                                    # passed through from scraper if present
  "all_image_urls": [...],                                # deduped
  "best_image": {
    "image_url": "<chosen url>",
    "reasoning": "<model's brief rationale>"
  }
}

Requirements:
- Python 3.9+
- `pip install openai python-dotenv` (if you want .env support)
- Environment variable: OPENAI_API_KEY
- Local modules: extract_url_info.py and generic_web_scraper.py in PYTHONPATH

Usage:
  python select_best_product_image.py "https://example.com/product"
  python select_best_product_image.py --max-images 20 "https://www.amazon.com/..."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Any, Optional
from urllib.parse import urlparse

# Optional: load .env if present
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# Local modules you mentioned
try:
    from extract_url_info import extract_with_gpt5
except ImportError as e:
    print("ERROR: Could not import extract_with_gpt5 from extract_url_info.py", file=sys.stderr)
    raise

try:
    # Your scraper signature: main(url: str, company: str) -> dict
    from generic_web_scraper import main as scrape_main
except ImportError as e:
    print("ERROR: Could not import main from generic_web_scraper.py", file=sys.stderr)
    raise

# OpenAI client (official SDK v1+ style)
try:
    from openai import OpenAI  # pip install openai
except ImportError:
    print("ERROR: Please `pip install openai`.", file=sys.stderr)
    raise

MODEL_ALIAS_EXPANDER = "gpt-5"         # you asked for GPT-5
MODEL_IMAGE_RANKER   = "gpt-5"         # same model for ranking
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    print("ERROR: OPENAI_API_KEY not set.", file=sys.stderr)
    sys.exit(1)

client = OpenAI(api_key=OPENAI_API_KEY)


def normalize_url_list(urls: List[str], max_images: Optional[int] = None) -> List[str]:
    """Dedupes, strips, basic filtering; optionally truncate."""
    seen = set()
    cleaned = []
    for u in urls:
        if not isinstance(u, str):
            continue
        s = u.strip()
        if not s:
            continue
        if s in seen:
            continue
        # very rough filter: require http(s)
        parsed = urlparse(s)
        if parsed.scheme not in ("http", "https"):
            continue
        seen.add(s)
        cleaned.append(s)
        if max_images and len(cleaned) >= max_images:
            break
    return cleaned


def expand_product_aliases_via_gpt5(seed_names: List[str]) -> List[str]:
    """
    Ask GPT-5 for concise, high-signal aliases/synonyms (no brand names, no model numbers).
    Return a deduped, lowercased list.
    """
    if not seed_names:
        return []

    prompt = (
        "You are helping expand concise product nouns for ranking images.\n"
        "Rules:\n"
        " - Return ONLY a JSON array of short names.\n"
        " - Include plural/singular variants if common (e.g., 'sofa','sofas').\n"
        " - Include things that you may also have with the object. For example couches may have pillows.\n"
        " - Exclude brands, model numbers, materials unless essential to identity.\n"
        " - Keep each item <= 3 words. No duplicates. Lowercase.\n\n"
        f"Seed names: {seed_names}\n"
    )

    resp = client.chat.completions.create(
        model=MODEL_ALIAS_EXPANDER,
        messages=[
            {"role": "system", "content": "You are a precise, terse product taxonomy assistant."},
            {"role": "user",   "content": prompt}
        ],
    )

    # The model may return an object; accept array if provided within it.
    content = resp.choices[0].message.content.strip()
    # Try array first; else try to find a key like {"aliases": [...]}
    aliases: List[str] = []
    try:
        obj = json.loads(content)
        if isinstance(obj, list):
            aliases = obj
        elif isinstance(obj, dict):
            # Flexible: look for the first list value
            for v in obj.values():
                if isinstance(v, list):
                    aliases = v
                    break
    except Exception:
        # If not valid JSON, try a last-ditch split; but we asked for JSON, so this is unlikely.
        aliases = []

    # Dedup + keep seeds too
    pool = {s.strip().lower() for s in seed_names if isinstance(s, str)}
    for a in aliases:
        if isinstance(a, str):
            pool.add(a.strip().lower())

    return sorted(pool)


def rank_images_with_gpt5(
    image_urls: List[str],
    product_names: List[str],
    dimensions: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Ask GPT-5 to pick the best image for the main object visibility criterion.

    Criteria restated:
     - Prefer an image where the primary object (as described by product_names) is fully visible.
     - Minimize objects IN FRONT OF or ON TOP OF the main object (occluders). Best is 0.
     - Measurement labels/overlays are OK.
     - Avoid images where other objects cover or obscure the main object.
     - If multiple ties, prefer front-facing, centered, good framing.
     - A measurement/diagram-style image if available is preferred over other images.

    Returns:
     {
       "image_url": str,
       "reasoning": str,
       "scores": { url: { "occlusion_score": int, "notes": str } }
     }
    """
    if not image_urls:
        return {"image_url": None, "reasoning": "No images provided.", "scores": {}}

    # Keep prompt compact—pass just the list and constraints. We assume the model can “see” URLs
    # only descriptively; it will reason based on textual cues like filenames or path tokens.
    # (If you want actual visual inspection, pair with a vision model that fetches images.)
    instruction = (
        "You are ranking candidate product images by how unobstructed the MAIN object is.\n"
        "Consider the product identity from the provided names. Measurement overlays are permitted.\n"
        "Hard rules:\n"
        " - Minimize objects covering/obscuring the main object (occlusions). Best is 0.\n"
        " - If tie: prefer front-facing, centered, entire object in frame.\n"
        " - Okay to have an image with measurement overlays.\n"
        " - Output strictly in JSON with keys: best_image_url, reasoning, scores.\n"
        "   Where 'scores' maps each URL to an object with: occlusion_score (integer; lower is better), notes.\n"
    )

    payload = {
        "product_names": product_names,
        "dimensions_hint": dimensions or {},
        "image_urls": image_urls
    }

    resp = client.chat.completions.create(
        model=MODEL_IMAGE_RANKER,
        messages=[
            {"role": "system", "content": "You are a meticulous product image judge."},
            {"role": "user", "content": instruction},
            {"role": "user", "content": f"Payload:\n{json.dumps(payload, ensure_ascii=False)}"}
        ],
    )

    content = resp.choices[0].message.content.strip()
    try:
        data = json.loads(content)
    except Exception:
        data = {}

    best = {
        "image_url": data.get("best_image_url"),
        "reasoning": data.get("reasoning", ""),
        "scores": data.get("scores", {})
    }
    return best


# pip install openai
import os
import json
from typing import List, Dict, Optional
from openai import OpenAI

def choose_dimensions_with_gpt(
    potential_dimension_values: List[str],
    model: str = "gpt-5",
) -> Dict[str, Optional[float]]:
    """
    Given a list of noisy dimension strings, returns:
    {
      "length": float|None,  # inches
      "width":  float|None,  # inches
      "height": float|None   # inches
    }
    """
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    user = (
        "Candidate dimension strings:\n"
        + "\n".join(f"- {s}" for s in potential_dimension_values)
        + "\n\nRules:\n"
          "- Prefer explicitly labeled product/item dimensions.\n"
          "- Prefer product/item over package/box dimensions.\n"
          "- Resolve synonyms: depth=breadth=width (unless clearly LxWxH triplet says otherwise); height is vertical.\n"
          "- Normalize to inches (1 in = 2.54 cm; 25.4 mm = 1 in).\n"
          "- Return ONLY JSON per the schema—no extra text.\n"
          "- Depth is the same as width.\n"
          "Return a json in the format: {length: <length value>, width: <width value>, height: <height value>}"
    )

    print("USER: ", user)

    resp = client.responses.create(
        model=model,
        input=[{"role": "user", "content": user}],
    )

    data = json.loads(resp.output_text)

    def _num(x):
        return None if x is None else float(x)

    return {
        "length": _num(data.get("length")),
        "width":  _num(data.get("width")),
        "height": _num(data.get("height")),
    }


import io
import requests
from PIL import Image

def save_best_image(image_url: str, out_path: str = "best_image.png") -> str:
    """
    Download image_url (any common format: jpg/png/webp/etc.) and save it as a PNG.
    Returns the output path. Raises on failure.
    """
    if not image_url:
        raise ValueError("image_url is empty.")

    # Fetch bytes (follow redirects, set UA to avoid some 403s)
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
    }
    r = requests.get(image_url, headers=headers, timeout=20)
    r.raise_for_status()

    # Decode and normalize to PNG
    img = Image.open(io.BytesIO(r.content))
    if img.mode not in ("RGB", "RGBA"):
        # If it has an alpha channel, keep it; otherwise convert to RGB
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")

    img.save(out_path, format="PNG", optimize=True)
    return out_path


def get_best_image_url(url):
    parser = argparse.ArgumentParser(description="Select the best product image with minimal occlusions.")
    # parser.add_argument("url", help="Product URL to analyze")
    parser.add_argument("--max-images", type=int, default=30, help="Cap the number of candidate image URLs")
    parser.add_argument("--print-scrape", action="store_true", help="Print raw scraper payload to stderr")
    args = parser.parse_args()

    # Amazon


    #Ikea
    # url = "https://www.ikea.com/us/en/p/uppland-sofa-blekinge-white-s19384116/"

    # Target
    # url = (
    #    "https://www.target.com/p/hyleory-112-in-w-4-piece-modern-corduroy-fabric-"
    #    "sectional-sofa-with-ottoman/-/A-92703198?preselect=1002282123#lnk=sametab"
    # )

    # Ebay
    # url = (
    #     "https://www.ebay.com/itm/286902766691?itmmeta=01K93Q62PKF8X7DHJKBFGP7SZE&h"
    #     "ash=item42ccbccc63:g:1-wAAeSw2Xho20DO&itmprp=enc%3AAQAKAAAA4NHOg0D50eDiCdi"
    #     "%2FfP0r02u0KKb67hWy%2BDkIlf97%2BERiD2%2BTwOXn1rGSKdwCoDzJO9Axt2hYA%2BL6gAo"
    #     "lxrirE%2BwaXe%2BMQaU%2BKIImK7FohURRnwjsqlI%2FIRfXAFCFuzwZS%2BrwMwiGu5koJr%"
    #     "2FG6E8Ml%2FbxcymkJf2zMbNXnntdI01TJISAC%2FoHRHOlzpfglClSQMjIjvTI3BIp84MOJtC"
    #     "T5px4fH3sABinwoz7EVoMLwtuoVEBacnRwGR9jJnyFJS7F50SOfrVdA5%2BNZTv2RHPlDG2YOO"
    #     "dlTL%2Bb3JSI7xXX7cp%7Ctkp%3ABk9SR7irmPfIZg"
    # )


    # 1) URL info via your helper (already uses GPT-5 per your description)
    info = extract_with_gpt5(url)
    if not isinstance(info, dict):
        print("ERROR: extract_with_gpt5 did not return a dict.", file=sys.stderr)
        sys.exit(2)

    company = info.get("company_name") or ""
    seed_names = info.get("product_name") or []
    if not isinstance(seed_names, list):
        seed_names = [str(seed_names)]

    # 2) Scrape using your scraper
    scrape_payload = scrape_main(url, company)
    if args.print_scrape:
        print("=== RAW SCRAPE PAYLOAD ===", file=sys.stderr)
        print(json.dumps(scrape_payload, indent=2, ensure_ascii=False), file=sys.stderr)

    print("Scrape payload: ", scrape_payload)

    # Parse JSON string -> dict
    data = json.loads(scrape_payload[0])

    # Get images
    image_urls = data.get("image_urls", [])

    original_dim = choose_dimensions_with_gpt(data.get("potential_dimension_values", []))

    dimensions = {
        "length": original_dim.get("length"),
        "width": original_dim.get("width"),
        "height": original_dim.get("height"),
    }

    # 3a) Expand aliases
    expanded_names = expand_product_aliases_via_gpt5(seed_names)

    # 3b) Rank images per your criteria
    best = rank_images_with_gpt5(image_urls, expanded_names, dimensions)

    # Final JSON result
    result = {
        "url": url,
        "company_name": company,
        "product_names": expanded_names,
        "dimensions": dimensions,
        "all_image_urls": image_urls,
        "best_image": {
            "image_url": best.get("image_url"),
            "reasoning": best.get("reasoning"),
        },
        "scores": best.get("scores", {})
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))

    save_best_image(best.get("image_url"), out_path="C:\\Users\\davin\\OneDrive\\Documents\\PreviewAR\\IS-Net\\best_image.png")


if __name__ == "__main__":
    get_best_image_url()
