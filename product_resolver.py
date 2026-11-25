"""Resolve a product directly from its Flipkart listing URL."""

import re

from request_flow import validate_product_url

# Where Flipkart's long SEO page title stops being the product name.
_TITLE_CUT = re.compile(r"\s+(?:Price in India|Online at|Buy\b|Online\b|Best Price|-\s*Flipkart|:\s*Flipkart|\|\s*Flipkart)",
                        re.I)

# Where the model name ends and the feature list begins:
# "boAt Sailor Nav Kids Watch, 2-way Video & Voice Calling, Geofencing" -> "boAt Sailor Nav Kids Watch"
_FEATURE_CUT = re.compile(r"\s*,\s*|\s+[|\u2013\u2014-]\s+|\s*\(|\s*\u2022\s*")


def short_product_name(title):
    """Short, exact product name from Flipkart's long page title.

    'boAt boAt Bassheads 100 Wired Earphones Wired Price in India - Buy boAt boAt Bassheads 100 Wired
     Earphones Wired Online - boAt : Flipkart.com'  ->  'boAt Bassheads 100 Wired Earphones'
    Steps: cut at the first marketing phrase, drop immediately repeated words ('boAt boAt'), and drop a
    trailing word that already appears earlier in the name ('... Wired Earphones Wired').
    Also cuts at the first comma / " - " / "|" / "(" so a feature list is never part of the name.
    Falls back to the original title if nothing usable is left."""
    text = " ".join(str(title or "").split())
    m = _TITLE_CUT.search(text)
    name = text[:m.start()].strip(" -:|,") if m else text
    cut = _FEATURE_CUT.search(name)
    if cut and name[:cut.start()].strip():      # keep the model name, drop the feature list
        name = name[:cut.start()].strip(" -:|,")
    words = []
    for w in name.split():
        if not words or w.lower() != words[-1].lower():
            words.append(w)
    if len(words) > 2 and words[-1].lower() in (x.lower() for x in words[:-1]):
        words.pop()
    return " ".join(words) or text


class ProductResolutionError(ValueError):
    pass


class ProductNotFoundError(ProductResolutionError):
    pass


def resolve_product(driver, product_url, timeout=20):
    """Open the supplied product URL and use Flipkart's page title as the product name."""
    from selenium.webdriver.support.ui import WebDriverWait
    product_url = validate_product_url(product_url)
    if not product_url:
        raise ProductNotFoundError("A Flipkart product URL is required")
    driver.get(product_url)
    WebDriverWait(driver, timeout).until(lambda d: d.execute_script("return document.readyState") == "complete")
    try:
        final_url = validate_product_url(driver.current_url)
    except ValueError:
        raise ProductResolutionError("Flipkart redirected the supplied URL to a non-product page") from None
    title = " ".join((driver.title or "").split()).split(" | Flipkart")[0].strip()
    if not title:
        raise ProductNotFoundError("Could not read the product title from the supplied Flipkart URL")
    # The page title is a long SEO string; the workbook, file name and e-mail use the short product name.
    return short_product_name(title), final_url