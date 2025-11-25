#!/usr/bin/env python3
import sys
import re
import time
import random
from pathlib import Path
from typing import Optional, Tuple

from dotenv import load_dotenv
from openai import OpenAI

from selenium import webdriver
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementNotInteractableException,
    WebDriverException,
    JavascriptException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

load_dotenv()

# ----------------------------------------------------------------------
# PRESS & HOLD / CAPTCHA HELPERS (Wayfair etc.)
# ----------------------------------------------------------------------

_PRESS_HOLD_XPATH = (
    "//*[@role='button' and contains(translate(normalize-space(.),"
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'press')"
    " and contains(translate(normalize-space(.),"
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'hold')]"
    " | /*//*[contains(translate(normalize-space(.),"
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','lowercase'),'press')"
    " and contains(translate(normalize-space(.),"
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','lowercase'),'hold')]"
)

def _find_in_iframes(driver, by, value, timeout: float = 6.0):
    """Return (element, frame_index or None)."""
    wait = WebDriverWait(driver, timeout)
    # main document
    try:
        el = wait.until(EC.presence_of_element_located((by, value)))
        return el, None
    except TimeoutException:
        pass

    # search in iframes
    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for i, fr in enumerate(frames):
        try:
            driver.switch_to.frame(fr)
            try:
                el = wait.until(EC.presence_of_element_located((by, value)))
                return el, i
            except TimeoutException:
                pass
        finally:
            driver.switch_to.default_content()
    return None, None


def _mouse_press_and_hold(driver, el, duration: float):
    actions = ActionChains(driver)
    actions.move_to_element(el).perform()
    time.sleep(0.12)
    actions.click_and_hold(el).perform()
    t_end = time.time() + duration
    while time.time() < t_end:
        ActionChains(driver).move_by_offset(
            random.randint(-2, 2), random.randint(-2, 2)
        ).perform()
        time.sleep(0.12 + random.random() * 0.15)
    ActionChains(driver).release(el).perform()


def _js_pointer_press_and_hold(driver, el, duration: float):
    js = r"""
const el = arguments[0];
const holdMs = Math.max(0, Math.floor(arguments[1]*1000));
el.scrollIntoView({block:'center', inline:'center'});
function fire(type, opts={}) {
  const r = el.getBoundingClientRect();
  el.dispatchEvent(new PointerEvent(type, Object.assign({
    bubbles:true, cancelable:true, composed:true,
    pointerId:1, pointerType:'mouse', isPrimary:true, buttons:1,
    clientX:(r.left+r.right)/2, clientY:(r.top+r.bottom)/2
  }, opts)));
}
function mouse(type){ el.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,buttons:1})); }
function touch(type){
  try{
    const r=el.getBoundingClientRect();
    const t=new Touch({identifier:1,target:el,clientX:(r.left+r.right)/2,clientY:(r.top+r.bottom)/2});
    el.dispatchEvent(new TouchEvent(type,{bubbles:true,cancelable:true,touches:[t],targetTouches:[t],changedTouches:[t]}));
  }catch(_){}
}
fire('pointerover'); fire('pointerenter'); fire('pointerdown');
mouse('mouseover'); mouse('mouseenter'); mouse('mousedown'); touch('touchstart');
return new Promise(res=>{ setTimeout(()=>{ fire('pointerup',{buttons:0}); mouse('mouseup'); mouse('click'); touch('touchend'); res(true); }, holdMs); });
"""
    driver.execute_script(js, el, duration)
    time.sleep(duration + 0.25)


def press_and_hold(
    driver,
    *,
    duration: float = 4.0,
    timeout: float = 8.0,
    locator: Optional[Tuple[str, str]] = None,
) -> bool:
    """
    Press & hold a button-like element.

    Args:
      driver    : Selenium WebDriver
      duration  : seconds to hold
      timeout   : seconds to search for element
      locator   : optional (By, value). If not given, finds element whose text contains
                  'press' and 'hold'.

    Returns:
      True if element found and hold executed; False otherwise.
    """
    by, val = locator if locator else (By.XPATH, _PRESS_HOLD_XPATH)

    # Find element (main or iframes)
    el, frame_idx = _find_in_iframes(driver, by, val, timeout=timeout)
    if not el:
        return False

    # Switch into the frame if needed
    if frame_idx is not None:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
        try:
            driver.switch_to.frame(frames[frame_idx])
        except Exception:
            driver.switch_to.default_content()

    # Ensure interactable and in view
    try:
        WebDriverWait(driver, 5).until(EC.element_to_be_clickable(el))
    except TimeoutException:
        pass
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center', inline:'center'});", el
    )
    time.sleep(0.15)

    # Try native, then JS fallback
    try:
        _mouse_press_and_hold(driver, el, duration)
    except WebDriverException:
        try:
            _js_pointer_press_and_hold(driver, el, duration)
        except JavascriptException:
            driver.switch_to.default_content()
            return False

    driver.switch_to.default_content()
    return True


# ----------------------------------------------------------------------
# AMAZON BOT SAFEGUARD HANDLER
# ----------------------------------------------------------------------

def handle_amazon_bot_safeguard(driver, timeout: int = 15) -> bool:
    """
    Handle Amazon's bot detection / safeguard buttons that appear before
    accessing the product page.

    Returns:
        bool: True if safeguard was handled successfully or not present, False otherwise
    """
    try:
        time.sleep(2)

        # Check if we're already on the product page (no safeguard needed)
        product_indicators = [
            "#productTitle",
            "#landingImage",
            "#productDetails_techSpec_section_1",
            "#add-to-cart-button",
            "[data-asin]",
        ]

        for indicator in product_indicators:
            try:
                driver.find_element(By.CSS_SELECTOR, indicator)
                print(f"Already on product page (found {indicator})")
                return True
            except NoSuchElementException:
                continue

        safeguard_patterns = [
            (By.XPATH, "//button[contains(., 'Continue shopping')]"),
            (By.XPATH, "//button[contains(., 'Show me the product')]"),
            (By.XPATH, "//button[contains(., 'Proceed')]"),
            (By.XPATH, "//button[contains(., 'Continue')]"),
            (By.XPATH, "//button[contains(., 'Try a different image')]"),
            (By.XPATH, "//a[contains(., 'Continue shopping')]"),
            (By.XPATH, "//a[contains(., 'Show me the product')]"),
            (By.XPATH, "//input[@type='submit' and contains(@value, 'Continue')]"),

            # CAPTCHA-related
            (By.CSS_SELECTOR, "button[id*='captcha']"),
            (By.CSS_SELECTOR, "button[class*='captcha']"),
            (By.CSS_SELECTOR, "button[id*='verify']"),
            (By.CSS_SELECTOR, "button[class*='verify']"),

            # Common Amazon safeguard button IDs and classes
            (By.CSS_SELECTOR, "#continue-button"),
            (By.CSS_SELECTOR, "#continue"),
            (By.CSS_SELECTOR, ".a-button-primary"),
            (By.CSS_SELECTOR, "button[data-action='continue']"),
            (By.CSS_SELECTOR, "button[aria-label*='Continue']"),

            # Generic submit buttons in forms
            (By.CSS_SELECTOR, "form button[type='submit']"),
            (By.CSS_SELECTOR, "form input[type='submit']"),
        ]

        button_clicked = False
        for by, selector in safeguard_patterns:
            try:
                element = WebDriverWait(driver, 3).until(
                    EC.presence_of_element_located((by, selector))
                )
                if element.is_displayed() and element.is_enabled():
                    print(f"Found safeguard button: {selector}")
                    driver.execute_script(
                        "arguments[0].scrollIntoView(true);", element
                    )
                    time.sleep(0.5)
                    try:
                        element.click()
                    except ElementNotInteractableException:
                        driver.execute_script("arguments[0].click();", element)

                    button_clicked = True
                    print("Clicked safeguard button")
                    break
            except (TimeoutException, NoSuchElementException):
                continue

        if button_clicked:
            time.sleep(3)
            for indicator in product_indicators:
                try:
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, indicator)
                        )
                    )
                    print(
                        f"Successfully navigated to product page (found {indicator})"
                    )
                    return True
                except TimeoutException:
                    continue

            current_url = driver.current_url
            if "amazon.com/dp/" in current_url or "amazon.com/product/" in current_url:
                print("URL suggests we're on a product page")
                return True

        if not button_clicked:
            print(
                "No safeguard button found - may already be on product page or safeguard not present"
            )
            for indicator in product_indicators:
                try:
                    driver.find_element(By.CSS_SELECTOR, indicator)
                    return True
                except NoSuchElementException:
                    continue

        return False

    except Exception as e:
        print(f"Error handling Amazon bot safeguard: {str(e)}")
        return False


# ----------------------------------------------------------------------
# RAG ANALYSIS WITH OPENAI
# ----------------------------------------------------------------------

def analyze_product_with_rag(html_content: str) -> str:
    """Use OpenAI to identify product dimensions and image URLs from HTML."""
    client = OpenAI()

    prompt = f"""
    You are a precise information extraction model. Extract product DIMENSIONS and IMAGE URLS from raw Amazon HTML.

    # INPUT
    The full HTML of the product page:
    <HTML>
    {html_content}
    </HTML>

    # TASK
    1) Find the product's physical dimensions anywhere in the HTML (bullets, specs tables, A+ content, etc.).
    2) Collect ALL product image URLs (primary + gallery). Prefer full-resolution URLs.

    # HOW TO EXTRACT
    - Consider common dimension locations:
      - Tech details/spec tables (e.g., "Product Dimensions", "Item Dimensions LxWxH")
      - Bullets and description blocks
      - Image blocks (e.g., image gallery JSON, "data-old-hires", "hiRes", "mainImageUrl")
    - Dimension patterns to look for (examples):
      - "Dimensions: 12.5 x 8 x 3 inches"
      - "Item Dimensions LxWxH: 10 x 5 x 2 in"
      - "Height: 15 cm", "Width: 8.2 in", "Length: 20 cm"
    - For dimensions given as LxWxH:
      - If units are in cm/mm, convert to inches (1 inch = 2.54 cm; 10 mm = 1 cm).
    - Do NOT return CSS selectors—return actual values and absolute image URLs.
    - Deduplicate image URLs.

    # IMAGE URL RULES
    - Prefer highest-resolution URLs available (fields like "hiRes", "data-old-hires", "mainImage", or the largest variant in gallery JSON).
    - If URLs are relative, resolve against the page origin (if unknown, return as-is).
    - Include ALL product images in the list; order with primary first if identifiable.

    # OUTPUT FORMAT (STRICT)
    Return ONLY a single JSON object. No prose, no markdown, no explanations.

    {{
      "potential_dimension_values": [<string>, ...],
      "image_urls": [<string>, ...]
    }}

    # CONVERSION & VALIDATION
    - Normalize units to inches in the strings above (e.g., "31.8 cm" -> "12.52 in").
    - If you can only partially infer, fill what you can and set the rest to null.
    - The JSON must be valid and parseable. No trailing commas. No comments in the JSON.
    """

    response = client.chat.completions.create(
        model="gpt-5",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


# ----------------------------------------------------------------------
# SELENIUM / SCRAPER CORE
# ----------------------------------------------------------------------

def init_chrome(headless: bool = True) -> webdriver.Chrome:
    """
    Initialize Chrome/Chromium with flags that are WSL/container-friendly.
    Default is headless=True for WSL.
    """
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1280,1024")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    # If you use Chromium in WSL, you may need to uncomment and adjust:
    # chrome_options.binary_location = "/usr/bin/chromium-browser"

    return webdriver.Chrome(options=chrome_options)


def scrape_and_analyze_url(
    url: str,
    *,
    company: Optional[str] = None,
    headless: bool = True,
    out_dir: str = ".",
    output_prefix: str = "page",
) -> Tuple[str, Path, Path]:
    """
    Navigate to `url`, dump raw HTML, filter to img/span/td/(li for Ikea),
    run RAG, and return:
      (analysis_result, raw_html_path, filtered_html_path)
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    driver = init_chrome(headless=headless)
    try:
        driver.get(url)

        # Wait for DOM ready
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

        # Only handle Amazon safeguard if company is explicitly Amazon
        if company and company.strip().lower() == "amazon":
            try:
                print("Checking for Amazon bot safeguard buttons...")
                handled = handle_amazon_bot_safeguard(driver)
                if handled:
                    print(
                        "Successfully handled bot safeguard or already on product page"
                    )
                else:
                    print(
                        "Warning: Bot safeguard may not have been fully handled, proceeding anyway..."
                    )
            except NameError:
                pass

        time.sleep(2)  # small buffer for dynamic content

        # --- Retrieve HTML ---
        html = driver.page_source
        raw_path = out_path / f"{output_prefix}.html"
        raw_path.write_text(html, encoding="utf-8")

        # safer Ikea detection
        is_ikea = bool(company and "ikea" in company.lower())
        if is_ikea:
            tags_group = "li|span|td"
        else:
            tags_group = "span|td"

        pattern = re.compile(
            rf"""
            # <img ...> (self-closing or not)
            <img\b[^>]*>
            |
            # <({tags_group}) ...> ... </same-tag>
            <({tags_group})\b[^>]*>         # \1 = tag name
                (?:
                    (?!</?\1\b)             # don't let the same tag start/end here
                    .
                )*?
            </\1>
            """,
            re.IGNORECASE | re.DOTALL | re.VERBOSE,
        )

        matches = [m.group(0) for m in pattern.finditer(html)]
        filtered_html = " ".join(matches)

        filtered_path = out_path / f"{output_prefix}_filtered.html"
        filtered_path.write_text(filtered_html, encoding="utf-8")

        # --- Analyze with RAG ---
        try:
            analysis_result = analyze_product_with_rag(filtered_html)
        except NameError:
            analysis_result = (
                "analyze_product_with_rag(filtered_html) not found.\n"
                "Provide your implementation or import it to get real results."
            )

        print("\n" + "=" * 50)
        print("PRODUCT ANALYSIS RESULT:")
        print("=" * 50)
        print(analysis_result)
        print("=" * 50)

        return analysis_result, raw_path, filtered_path

    finally:
        time.sleep(1)
        driver.quit()


def main(url: Optional[str] = None, company: Optional[str] = None) -> Tuple[str, Path, Path]:
    """
    Pass a URL and company into the scraper/analyzer.
    Only triggers Amazon safeguard logic when company == 'Amazon'.

    In WSL / headless environments, we force headless=True here.
    """
    if url is None:
        raise ValueError("URL must not be None")
    return scrape_and_analyze_url(
        url,
        company=company,
        headless=True,     # <-- HEADLESS ON FOR WSL
        out_dir=".",
        output_prefix="page",
    )


if __name__ == "__main__":
    # Example Wayfair URL (change as needed):
    url = (
        "https://www.wayfair.com/furniture/pdp/latitude-run-cenie-modern-upholstered"
        "-arc-shaped-3d-knit-fabric-sofa-no-assembly-required3-seat-w115334476.html?"
        "piid=224002162"
    )

    # Example:
    #   python generic_web_scraper.py "https://www.amazon.com/..." Amazon
    if len(sys.argv) >= 2:
        url = sys.argv[1]
    company = sys.argv[2] if len(sys.argv) >= 3 else "Ikea"

    main(url, company)
