#!/usr/bin/env python3
"""
prompt_generator.py

Given a product page URL, this module:

1) Uses extract_url_info.extract_with_gpt5(url) to get the target object name(s)
   (e.g., ["sectional", "couch", "sofa"]) and picks a main target object
   (e.g., "couch").

2) Uses best_image_selector.get_best_image_url(url) to get the URL of the
   best product image.

3) Uses a GPT vision call to inspect that best image and identify objects
   that are NOT the target object (e.g., "blanket", "pillow", "person",
   "coffee table", "clutter", "text", "watermark", etc.).

4) Returns:
     target_object, negative_prompt

   where:
     target_object: str, e.g. "couch"
     negative_prompt: str, comma-separated list, e.g.
       "blanket, pillow, person, clutter, text, watermark"
"""

from __future__ import annotations

import json
import os
import sys
from typing import Tuple, List

from dotenv import load_dotenv  # type: ignore
from openai import OpenAI  # pip install openai

# Load env vars (for OPENAI_API_KEY)
load_dotenv()

# ---------------------------------------------------------------------
# Imports from your existing modules
# ---------------------------------------------------------------------
try:
    # best_image_selector.py should define:
    #   def get_best_image_url(url: str, max_images: int = 30) -> str
    from best_image_selector import get_best_image_url
except ImportError:
    print(
        "ERROR: Could not import get_best_image_url from best_image_selector.py",
        file=sys.stderr,
    )
    raise

try:
    # extract_url_info.py defines:
    #   def extract_with_gpt5(url: str) -> Dict[str, Any]
    from extract_url_info import extract_with_gpt5
except ImportError:
    print(
        "ERROR: Could not import extract_with_gpt5 from extract_url_info.py",
        file=sys.stderr,
    )
    raise


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    print("ERROR: OPENAI_API_KEY not set in environment.", file=sys.stderr)
    sys.exit(1)


class PromptGenerator:
    """
    High-level helper:

        generator = PromptGenerator()
        target_object, negative_prompt = generator.generate_target_and_negative(url)

    This will:
      - determine the main product object from the URL ("couch", "table", ...)
      - pick the best product image via best_image_selector
      - inspect that image and list all non-target objects as a negative prompt
    """

    def __init__(self, model: str = "gpt-5"):
        self.model = model
        self.client = OpenAI(api_key=OPENAI_API_KEY)

    # ----------------------- PUBLIC API ----------------------- #
    def generate_target_and_negative(self, url: str) -> Tuple[str, str]:
        """
        Main entry point.

        Args:
            url: Product URL.

        Returns:
            (target_object, negative_prompt)

        Example:
            ("couch",
             "blanket, pillow, person, rug, coffee table, clutter, text, watermark")
        """

        # 1) Get target object from extract_url_info
        info = extract_with_gpt5(url)
        if not isinstance(info, dict):
            raise RuntimeError("extract_with_gpt5 did not return a dict.")

        product_names = info.get("product_name") or []
        if not isinstance(product_names, list):
            product_names = [str(product_names)]

        # Pick a main target object string; fall back to "product" if missing
        target_object = (
            str(product_names[0]) if product_names else "product"
        ).strip()

        # 2) Get best image URL from best_image_selector
        best_image_url = get_best_image_url(url)
        if not best_image_url:
            # No best image found → empty negative prompt
            return target_object, ""

        # 3) Ask GPT (vision) which objects in the image are NOT the target
        negative_prompt = self._generate_negative_prompt_from_image(
            image_url=best_image_url,
            target_object=target_object,
        )

        return target_object, negative_prompt

    # -------------------- INTERNAL HELPERS -------------------- #
    def _generate_negative_prompt_from_image(
        self,
        image_url: str,
        target_object: str,
    ) -> str:
        """
        Use GPT vision to inspect the best image and list objects that are NOT the
        main target object, suitable for a negative prompt.

        Returns a comma-separated string like:
          "blanket, pillow, person, clutter, text, watermark"
        """
        if not image_url:
            return ""

        messages = [
            {
                "role": "system",
                "content": "You are a concise vision assistant that outputs strict JSON only.",
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "You are given a product image and the target object name.\n"
                            f"Target/main object: '{target_object}'.\n\n"
                            "1) Look at the image and identify physical objects that are NOT the target object.\n"
                            "   Examples for a couch: blankets, pillows, throws, people, pets, tables, lamps, rugs, "
                            "   background furniture, decor, plants, clutter, text, logos, watermarks, reflections.\n"
                            "2) Only list objects that could reasonably be suppressed or excluded when inpainting "
                            "   or generating images.\n"
                            "3) Do NOT include the main target object or clear synonyms of it.\n"
                            "4) Return ONLY JSON of the form:\n"
                            '   {\"negative_objects\": [\"blanket\", \"pillow\", \"person\", ...]}\n'
                            "   No extra text, no markdown."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    },
                ],
            },
        ]

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
        )

        content = (resp.choices[0].message.content or "").strip()

        negative_objects: List[str] = []
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                raw_list = data.get("negative_objects", [])
                if isinstance(raw_list, list):
                    negative_objects = [
                        str(x).strip() for x in raw_list if str(x).strip()
                    ]
        except Exception:
            negative_objects = []

        # Deduplicate, lowercase, join with commas
        seen = set()
        cleaned: List[str] = []
        for obj in negative_objects:
            o = obj.lower()
            if o and o not in seen:
                seen.add(o)
                cleaned.append(o)

        return ", ".join(cleaned)


# ---------------------------------------------------------------------
# Simple main(url: str) wrapper
# ---------------------------------------------------------------------
def main(url: str) -> Tuple[str, str]:
    """
    Convenience function so you can just do:

        from prompt_generator import main
        target, negative = main(url)

    Returns:
        (target_object, negative_prompt)
    """
    generator = PromptGenerator()
    return generator.generate_target_and_negative(url)


# ---------------------------------------------------------------------
# Optional: script usage (for quick manual testing)
# ---------------------------------------------------------------------
if __name__ == "__main__":
    # You can temporarily hard-code a URL here for testing,
    # or modify this to read from sys.argv if you like.
    test_url = "https://www.amazon.com/Modular-Sectional-L-Shape-Assembly-Required/dp/B0F7F1XPCG"
    target, negative = main(test_url)
    print("TARGET OBJECT:", target)
    print("NEGATIVE PROMPT:", negative)
