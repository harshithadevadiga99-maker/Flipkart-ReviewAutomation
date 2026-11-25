# v7

"""Selenium layer. Every step is defensive: it retries, falls back, and never hangs the run."""

import logging

import re

import threading

import time

from datetime import datetime

from pathlib import Path



from selenium import webdriver

from selenium.common.exceptions import WebDriverException

from selenium.webdriver.common.by import By

from selenium.webdriver.support.ui import WebDriverWait



from fk_parsing import build_review_url, classify_page, parse_keyword_percentages, parse_reviews_detailed



log = logging.getLogger("fk")





# --------------------------------------------------------------------------- #

# Driver + small utilities

# --------------------------------------------------------------------------- #

def make_driver(headless=False, profile_dir=None, block_images=False):

    opts = webdriver.ChromeOptions()

    if headless:

        opts.add_argument("--headless=new")

    if profile_dir:

        profile_path = Path(profile_dir)

        if not profile_path.is_absolute():

            profile_path = Path(__file__).resolve().parent / profile_path

        profile_path.mkdir(parents=True, exist_ok=True)

        opts.add_argument(f"--user-data-dir={profile_path}")

    opts.add_argument("--window-size=1400,1000")

    opts.add_argument("--disable-notifications")

    opts.add_argument("--disable-blink-features=AutomationControlled")

    opts.add_argument("--lang=en-IN")

    if block_images:                       # reviews are text: skipping images/fonts makes every load faster

        opts.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})

    # Selenium >= 4.6 downloads the matching chromedriver automatically (Selenium Manager).

    drv = webdriver.Chrome(options=opts)

    drv.set_page_load_timeout(60)

    drv.set_script_timeout(30)          # a stuck JS call must fail in 30 s, not hang the whole run

    try:                                 # ...and so must any single driver command (default is 120 s+)

        drv.command_executor.set_timeout(90)

    except Exception:

        pass

    return drv





def page_text(driver):

    try:

        return driver.execute_script("return document.body ? document.body.innerText : ''") or ""

    except WebDriverException:

        return ""





def wait_for_content(driver, timeout=25, min_chars=200):

    try:

        WebDriverWait(driver, timeout).until(lambda d: len(page_text(d)) > min_chars)

        return True

    except Exception:

        return False





def js_click(driver, el):

    try:

        el.click()

    except Exception:

        driver.execute_script("arguments[0].click();", el)





def dump_diagnostics(driver, diag_dir, tag):

    """Save HTML + screenshot so a broken selector can be fixed in minutes."""

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", tag)[:40]

    base = diag_dir / f"{safe}_{stamp}"

    try:

        (base.with_suffix(".html")).write_text(driver.page_source, encoding="utf-8")

        driver.save_screenshot(str(base.with_suffix(".png")))

        log.info("Diagnostics saved: %s.[html|png]", base.name)

        return str(base)

    except Exception as exc:

        log.warning("Could not save diagnostics: %s", exc)

        return None





def safe_get(driver, url, retries=2, wait=25):

    last = None

    for attempt in range(1, retries + 2):

        try:

            driver.get(url)

            if wait_for_content(driver, wait):

                return True

        except WebDriverException as exc:

            last = exc

        log.warning("Load attempt %d failed for %s", attempt, url[:80])

        time.sleep(2 * attempt)

    if last:

        log.warning("Last load error: %s", str(last)[:150])

    return False





def _visible(driver, xpath):

    return [e for e in driver.find_elements(By.XPATH, xpath) if e.is_displayed()]





# Login lives in fk_login.py (login_to_flipkart).





# --------------------------------------------------------------------------- #

# Live page adapter. fk_collect.py talks to this interface only, so the whole

# pipeline can be tested offline with a fake page.

# --------------------------------------------------------------------------- #

_DATE_COUNT_JS = ("return (document.body.innerText.match(/\\d+\\s+(?:day|week|month|year)s?\\s+ago/gi)||[]).length")



# Click ONLY the 'more' control that follows a text span ending in an ellipsis

# (never the capital-M "More" menu in the site header).

_EXPAND_JS = """

let n=0;

document.querySelectorAll('span,div').forEach(e=>{

  if(e.children.length!==0) return;

  if((e.textContent||'').trim()!=='more') return;

  const p=e.previousElementSibling;

  if(p && /(\\.\\.\\.|\\u2026)\\s*$/.test(p.textContent||'')){ try{e.click(); n++;}catch(_){} }

});

return n;"""



# --------------------------------------------------------------------------- #

# Deep expansion of truncated reviews.

# Flipkart cuts long reviews ("... more").  The inline 'more' link often does nothing, so a cut-off review is

# opened another way: (1) click 'more', (2) click one of the review's photos -> the photo viewer shows the text

# with a 'Read more' link, (3) click the excerpt of a topic-list card -> a 'Complete Review' side panel opens,

# (4) click the review text itself.  The full text is then written back into the live page, so the normal

# parser reads the complete review.  If nothing works the visible (partial) text is kept.

# --------------------------------------------------------------------------- #

EXPAND_STATS = {"expanded": 0, "inline": 0, "photo_viewer": 0, "complete_review_panel": 0, "text_click": 0,

                "not_expanded": 0}

_EXPAND_LOCK = threading.Lock()



# Find review cards whose text is cut off and tag them (data-fk-card / data-fk-body / data-fk-id).

# Returns [{id, prefix, vis, kind('more'|'ellipsis'), title}]

_TRUNC_SCAN_JS = r"""

const norm=t=>(t||'').replace(/\s+/g,' ').trim();

const ELL=/(\.\.\.|\u2026)\s*$/, MORE=/^(read\s+)?more$/i, VER=/(verified purchase|certified buyer)/i;

const RATE=/^[1-5](\.0)?$/;

const shown=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);

const limit=arguments[0]||40; let nextId=arguments[1]||1; const out=[];

const leaves=[...document.querySelectorAll('span,div,p,a')].filter(e=>e.children.length===0&&shown(e));

const seen=new Set();

const verCount=n=>((n.innerText||'').match(/verified purchase|certified buyer/gi)||[]).length;

const findCard=leaf=>{let n=leaf;for(let i=0;i<12&&n.parentElement;i++){n=n.parentElement;const c=verCount(n);

  if(c===1)return n; if(c>1)return null;}return null;};

for(const e of leaves){

  if(out.length>=limit) break;

  const t=norm(e.textContent); let body=null, kind=null;

  if(t==='more'||/^read more$/i.test(t)){

    const p=e.previousElementSibling;

    if(p && ELL.test(norm(p.textContent))){body=p;kind='more';}

    else if(p && norm(p.textContent).length>20){body=p;kind='more';}

  } else if(t.length>12 && ELL.test(t)){

    body=e;kind='ellipsis';

  }

  if(!body||seen.has(body)||body.hasAttribute('data-fk-done')) continue;

  const card=findCard(body); if(!card) continue;

  const cardText=norm(card.innerText||'');

  if(kind==='ellipsis'){

    if([...card.querySelectorAll('span,div')].some(x=>x.children.length===0&&MORE.test(norm(x.textContent)))) continue;

    if([...card.querySelectorAll('span,div')].some(x=>x.children.length===0&&RATE.test(norm(x.textContent)))) continue;

  }

  seen.add(body);

  const bt=norm(body.textContent).replace(/(\.\.\.|\u2026)\s*$/,'').trim();

  const id=String(nextId++);

  card.setAttribute('data-fk-card',id); body.setAttribute('data-fk-body',id);

  const prev=body.previousElementSibling; const title=prev?norm(prev.textContent):'';

  out.push({id:id,prefix:bt.slice(0,40),vis:bt.length,kind:kind,title:title.length<=120?title:''});

}

return out;"""



_INLINE_CLICK_JS = r"""

const card=document.querySelector('[data-fk-card="'+arguments[0]+'"]'); if(!card) return false;

const more=[...card.querySelectorAll('span,div,a')].find(x=>x.children.length===0&&/^(read\s+)?more$/i.test((x.textContent||'').trim()));

if(!more) return false; more.scrollIntoView({block:'center'}); more.click(); return true;"""



_BODY_STATE_JS = r"""

const b=document.querySelector('[data-fk-body="'+arguments[0]+'"]'); if(!b) return null;

const card=document.querySelector('[data-fk-card="'+arguments[0]+'"]');

const more=card&&[...card.querySelectorAll('span,div,a')].some(x=>x.children.length===0&&/^(read\s+)?more$/i.test((x.textContent||'').trim()));

return {text:(b.innerText||b.textContent||'').replace(/\s+/g,' ').trim(), more:!!more};"""



_CLICK_IMAGE_JS = r"""

const card=document.querySelector('[data-fk-card="'+arguments[0]+'"]'); if(!card) return false;

const imgs=[...card.querySelectorAll('img')].filter(i=>{

  const w=i.offsetWidth||parseInt(i.getAttribute('width')||'0')||0, h=i.offsetHeight||parseInt(i.getAttribute('height')||'0')||0;

  return (w>=30&&h>=30)||/rukminim|flixcart|flipkart/i.test(i.currentSrc||i.src||'');});

if(!imgs.length) return false;

imgs[0].scrollIntoView({block:'center'}); imgs[0].click(); return true;"""



_CLICK_BODY_JS = r"""

const b=document.querySelector('[data-fk-body="'+arguments[0]+'"]'); if(!b) return false;

b.scrollIntoView({block:'center'}); b.click(); return true;"""



# Photo viewer / overlay: text outside the card that begins with the review excerpt.  Optionally click its

# 'Read more', then return the longest block of text that still belongs to the review body.

_OVERLAY_READ_JS = r"""

const norm=t=>(t||'').replace(/\s+/g,' ').trim();

const id=arguments[0], prefix=arguments[1], title=arguments[2]||'', clickMore=!!arguments[3];

const card=document.querySelector('[data-fk-card="'+id+'"]');

const outside=e=>!(card&&card.contains(e));

const shown=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);

const all=[...document.querySelectorAll('span,div,p,a,button')].filter(e=>outside(e)&&shown(e));

const holders=all.filter(e=>norm(e.textContent).includes(prefix)&&e.children.length<=3);

let clicked=false;

if(clickMore){

  const tgt=all.find(e=>e.children.length===0&&/^read\s+more$/i.test(norm(e.textContent)))

    ||all.find(e=>e.children.length<=1&&/read\s+more$/i.test(norm(e.textContent))&&norm(e.textContent).includes(prefix))

    ||all.find(e=>e.children.length===0&&norm(e.textContent)==='more'&&holders.some(h=>h.contains(e)||(h.parentElement&&h.parentElement.contains(e))));

  if(tgt){tgt.click();clicked=true;}

}

const leaf=holders.sort((a,b)=>norm(a.textContent).length-norm(b.textContent).length)[0];

if(!leaf) return {found:false,clicked:clicked};

let node=leaf, best=leaf;

for(let i=0;i<3&&node.parentElement;i++){

  const par=node.parentElement, pt=norm(par.innerText||par.textContent);

  if(/(helpful|report abuse|certified buyer|verified purchase)/i.test(pt)) break;

  if(!pt.startsWith(prefix)) break;          // the parent also holds a title/badge before the body: stop

  if(title && pt.includes(title) && !norm(node.innerText||node.textContent).includes(title)) break;

  best=par; node=par;

}

let txt=norm(best.innerText||best.textContent);

txt=txt.replace(/\s*(\.\.\.|\u2026)*\s*(read\s+)?more\s*$/i,'').replace(/\s*(read\s+)?less\s*$/i,'').trim();

return {found:true,clicked:clicked,text:txt};"""



# 'Complete Review' side panel (topic lists)

_PANEL_READ_JS = r"""

const norm=t=>(t||'').replace(/\s+/g,' ').trim();

const prefix=arguments[0];

const hdr=[...document.querySelectorAll('span,div,p,h1,h2,h3')].find(e=>e.children.length===0&&/^complete review$/i.test(norm(e.textContent))&&(e.offsetWidth||e.offsetHeight));

if(!hdr) return {found:false};

let root=hdr; for(let i=0;i<6&&root.parentElement;i++){root=root.parentElement;if(/(certified buyer|verified purchase)/i.test(root.innerText||''))break;}

const leaves=[...root.querySelectorAll('span,div,p')].filter(e=>e.children.length===0);

let hit=leaves.filter(e=>norm(e.textContent).startsWith(prefix)||norm(e.textContent).includes(prefix))

               .sort((a,b)=>norm(b.textContent).length-norm(a.textContent).length)[0];

if(!hit) return {found:true,text:''};

return {found:true,text:norm(hit.innerText||hit.textContent)};"""



_OVERLAY_OPEN_JS = r"""

const norm=t=>(t||'').replace(/\s+/g,' ').trim();

const id=arguments[0], prefix=arguments[1];

const card=document.querySelector('[data-fk-card="'+id+'"]');

const shown=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);

if([...document.querySelectorAll('span,div,p')].some(e=>e.children.length===0&&/^complete review$/i.test(norm(e.textContent))&&shown(e))) return 'panel';

const out=[...document.querySelectorAll('span,div,p')].some(e=>e.children.length<=3&&shown(e)&&!(card&&card.contains(e))

     &&norm(e.textContent).includes(prefix)&&!e.closest('[data-fk-card]'));

return out?'viewer':'';"""



_CLOSE_OVERLAY_JS = r"""

const shown=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);

const sels='[aria-label*="close" i],[aria-label*="back" i],[title*="close" i],button[class*="close" i]';

const c=[...document.querySelectorAll(sels)].filter(shown)[0];

if(c){c.click();return 'button';}

const x=[...document.querySelectorAll('span,div,button')].find(e=>e.children.length===0&&shown(e)&&/^(\u00d7|\u2715|\u2716|x)$/i.test((e.textContent||'').trim()));

if(x){x.click();return 'x';}

const el=document.elementFromPoint(6, Math.round(window.innerHeight/2));

if(el){el.click();return 'backdrop';}

return '';"""



_APPLY_JS = r"""

const b=document.querySelector('[data-fk-body="'+arguments[0]+'"]'); if(!b) return false;

b.textContent=arguments[1];

const card=document.querySelector('[data-fk-card="'+arguments[0]+'"]');

if(card){[...card.querySelectorAll('span,div,a')].forEach(x=>{if(x.children.length===0&&/^(read\s+)?more$/i.test((x.textContent||'').trim()))x.remove();});}

b.setAttribute('data-fk-done','1'); return true;"""



_MARK_DONE_JS = "const b=document.querySelector('[data-fk-body=\"'+arguments[0]+'\"]'); if(b) b.setAttribute('data-fk-done','1'); return true;"



# Flipkart loads more reviews when the 'Hang on, loading content' sentinel scrolls into view.



_SCROLL_JS = """

const s=[...document.querySelectorAll('div')].find(e=>e.children.length===0&&/loading content/i.test(e.textContent||''));

if(s) s.scrollIntoView({block:'end'});

window.scrollTo(0, document.body.scrollHeight);

return !!s;"""

_NUDGE_UP_JS = "window.scrollBy(0, -Math.round(window.innerHeight * 1.5)); return true;"



_TOPICS_JS = """

const norm=t=>(t||'').replace(/\\s+/g,' ').trim();

const anchors=[...document.querySelectorAll('button,[role=button],a,div,span')]

  .filter(e=>e.children.length===0&&norm(e.textContent)==='Overall');

for(const a of anchors){

  let node=a;

  for(let i=0;i<4&&node&&node.parentElement;i++){

    node=node.parentElement;

    const labels=[...node.children].map(c=>norm(c.innerText||c.textContent)).filter(t=>t&&t.length<=30);

    if(labels.length>=3&&labels.includes('Overall')) return labels;

  }

}

return [];"""



# Click a chip by its exact label, but only the one that sits in the chip ROW (a container whose children

# include several of the sibling labels). This avoids matching the same words elsewhere on the page,

# e.g. 'Latest' / 'Positive' in review text or 'Sound Quality' in a keyword chip.

# Third argument `strict`: when true there is NO fallback to "any element with that text" - if the chip row

# does not contain the label the function returns false (the caller can then wait and look again).

_CLICK_CHIP_JS = """

const norm=t=>(t||'').replace(/\\s+/g,' ').trim();

const label=arguments[0], group=arguments[1]||[], strict=!!arguments[2];

const need=Math.min(3, group.length);

const shown=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);

const cands=[...document.querySelectorAll('button,[role=button],a,div,span')]

  .filter(e=>e.children.length===0&&norm(e.textContent)===label&&shown(e));

let target=null;

for(const c of cands){

  let node=c;

  for(let i=0;i<5&&node&&node.parentElement&&!target;i++){

    node=node.parentElement;

    const texts=[...node.children].map(k=>norm(k.innerText||k.textContent));

    if(texts.includes(label)&&group.filter(g=>texts.includes(g)).length>=need) target=c;

  }

  if(target) break;

}

if(!target&&!strict&&cands.length) target=cands[0];

if(!target) return false;

target.scrollIntoView({block:'center'});

target.click();

return true;"""



_SORT_LABELS = {

    "MOST_RECENT": "Latest",

    "MOST_HELPFUL": "Most Helpful",

    "POSITIVE_FIRST": "Positive",

    "NEGATIVE_FIRST": "Negative",

}

_SORT_GROUP = ["Most Helpful", "Latest", "Positive", "Negative"]





class LivePage:

    """Selenium-backed implementation of the page interface used by fk_collect."""



    def __init__(self, driver, settings, diag_dir, selectors=None):

        self.driver, self.settings, self.diag_dir = driver, settings, diag_dir

        self.selectors = selectors or {}

        self.reviews_url = ""          # the reviews page this browser opened (used by reload_reviews)

        self._snapped = set()          # failure snapshots already taken (one per tag per browser)



    # -- basic state -------------------------------------------------------

    @property

    def url(self):

        try:

            return self.driver.current_url

        except WebDriverException:

            return ""



    @property

    def title(self):

        try:

            return self.driver.title

        except WebDriverException:

            return ""



    def text(self):

        return page_text(self.driver)



    def html(self):

        return self.driver.page_source



    def date_count(self):

        """Count review entries, including entries whose dates are absolute."""

        return self.review_count()



    def review_count(self):

        """Number of review cards in the DOM.



        Fast path: count the configured review blocks with one tiny JS call instead of re-parsing the

        whole page source (which got slower with every scroll round). Falls back to the full parser

        when the configured selector matches nothing (e.g. topic cards or a changed layout)."""

        try:

            blocks = (self.selectors or {}).get("review_block") or []

            for css in blocks:

                n = int(self.driver.execute_script("return document.querySelectorAll(arguments[0]).length", css) or 0)

                if n:

                    return n

            if self.selectors:

                return len(parse_reviews_detailed(self.html(), self.selectors)["reviews"])

            return int(self.driver.execute_script(_DATE_COUNT_JS) or 0)

        except Exception:

            return 0



    def wait_for_review_entries(self, timeout):

        end = time.time() + timeout

        while time.time() < end:

            count = self.review_count()

            if count > 0:

                return count

            time.sleep(0.5)

        return self.review_count()



    # -- actions -----------------------------------------------------------

    def expand_more(self):

        try:

            n = int(self.driver.execute_script(_EXPAND_JS) or 0)

        except WebDriverException:

            return 0

        if n:

            time.sleep(0.6)

        if self.settings.get("expand_truncated_reviews", True) and getattr(self, "_deep_ok", True):

            try:

                n += self._expand_truncated()

            except WebDriverException as exc:

                log.debug("deep expansion interrupted: %s", str(exc)[:100])

        return n



    # -- deep expansion of cut-off reviews ---------------------------------

    def _js(self, script, *args):

        return self.driver.execute_script(script, *args)



    def _press_escape(self):

        try:

            from selenium.webdriver.common.action_chains import ActionChains

            from selenium.webdriver.common.keys import Keys

            ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()

        except WebDriverException:

            pass



    def _overlay_state(self, rid, prefix):

        try:

            return self._js(_OVERLAY_OPEN_JS, rid, prefix) or ""

        except WebDriverException:

            return ""



    def _close_overlay(self, rid, prefix, url_before):

        """Close a photo viewer / 'Complete Review' panel: Esc, then a close/back button, then the backdrop."""

        for step in range(3):

            if not self._overlay_state(rid, prefix):

                break

            if step == 0:

                self._press_escape()

            else:

                try:

                    self._js(_CLOSE_OVERLAY_JS)

                except WebDriverException:

                    pass

            time.sleep(0.5)

        if self.url != url_before:                       # the viewer was a page of its own: go back to the list

            try:

                self.driver.back()

                time.sleep(1.5)

            except WebDriverException:

                pass

        return not self._overlay_state(rid, prefix)



    def _full_text_ok(self, full, prefix, vis):

        full = (full or "").strip()

        return bool(full) and len(full) > vis + 3 and full.lower().startswith(prefix.lower()[:25])



    def _expand_truncated(self):

        """Open every cut-off review another way and write its complete text back into the page."""

        if not hasattr(self, "_full_cache"):

            self._full_cache, self._next_card_id, self._deep_failures = {}, 1, 0

        limit = int(self.settings.get("expand_max_per_round", 40))

        cards = self._js(_TRUNC_SCAN_JS, limit, self._next_card_id) or []

        if not cards:

            return 0

        self._next_card_id += len(cards) + 1

        budget_end = time.time() + float(self.settings.get("expand_round_seconds", 90))

        done = 0

        for c in cards:

            rid, prefix, vis = c["id"], c["prefix"], c["vis"]

            if len(prefix) < 12:

                self._js(_MARK_DONE_JS, rid)

                continue

            key = (prefix, vis)

            if key in self._full_cache:                      # same review seen before: reuse its full text

                self._js(_APPLY_JS, rid, self._full_cache[key])

                done += 1

                continue

            if time.time() > budget_end or not getattr(self, "_deep_ok", True):

                break

            before = self.review_count()

            full, how = "", ""

            try:

                full, how = self._expand_one(c)

            except WebDriverException as exc:

                log.debug("expanding review %s failed: %s", rid, str(exc)[:100])

            if full:

                self._full_cache[key] = full

                if self._js(_APPLY_JS, rid, full):

                    done += 1

                    with _EXPAND_LOCK:

                        EXPAND_STATS["expanded"] += 1

                        EXPAND_STATS[how] = EXPAND_STATS.get(how, 0) + 1

            else:

                self._js(_MARK_DONE_JS, rid)                 # keep the visible (partial) text, do not retry forever

                with _EXPAND_LOCK:

                    EXPAND_STATS["not_expanded"] += 1

                log.info("review %s could not be expanded; keeping the visible text", rid)

            # safety: opening a viewer must never cost us the list we already loaded

            after = self.review_count()

            if after + 2 < before:

                self._deep_failures += 1

                log.warning("review list shrank from %d to %d after expanding a review (%d time(s))",

                            before, after, self._deep_failures)

                if self._deep_failures >= 2:

                    self._deep_ok = False

                    log.warning("deep expansion switched off for this page to protect the collected list")

                    break

        return done



    def _expand_one(self, c):

        rid, prefix, vis, kind, title = c["id"], c["prefix"], c["vis"], c["kind"], c.get("title", "")

        wait = float(self.settings.get("expand_wait_seconds", 3))

        url_before = self.url



        def poll(fn, timeout):

            end = time.time() + timeout

            while time.time() < end:

                val = fn()

                if val:

                    return val

                time.sleep(0.25)

            return None



        # 1) the inline 'more' link

        if kind == "more" and self._js(_INLINE_CLICK_JS, rid):

            time.sleep(0.6)

            st = self._js(_BODY_STATE_JS, rid)

            if st and self._full_text_ok(st["text"], prefix, vis) and not st["more"]:

                return st["text"], "inline"



        # 2) a photo of the review -> photo viewer with the text and a 'Read more' link

        if self._js(_CLICK_IMAGE_JS, rid):

            if poll(lambda: self._overlay_state(rid, prefix), wait):

                res = self._js(_OVERLAY_READ_JS, rid, prefix, title, True) or {}

                if res.get("clicked"):

                    time.sleep(0.6)

                    res = self._js(_OVERLAY_READ_JS, rid, prefix, title, False) or res

                full = res.get("text", "")

                self._close_overlay(rid, prefix, url_before)

                if self._full_text_ok(full, prefix, vis):

                    return full, "photo_viewer"

            else:

                self._close_overlay(rid, prefix, url_before)



        # 3) topic-list card or plain text: click the text -> 'Complete Review' panel

        if self._js(_CLICK_BODY_JS, rid):

            state = poll(lambda: self._overlay_state(rid, prefix), wait)

            if state == "panel":

                res = self._js(_PANEL_READ_JS, prefix) or {}

                full = res.get("text", "")

                self._close_overlay(rid, prefix, url_before)

                if self._full_text_ok(full, prefix, vis):

                    return full, "complete_review_panel"

            elif state == "viewer":

                res = self._js(_OVERLAY_READ_JS, rid, prefix, title, True) or {}

                if res.get("clicked"):

                    time.sleep(0.6)

                    res = self._js(_OVERLAY_READ_JS, rid, prefix, title, False) or res

                full = res.get("text", "")

                self._close_overlay(rid, prefix, url_before)

                if self._full_text_ok(full, prefix, vis):

                    return full, "text_click"

            else:

                self._close_overlay(rid, prefix, url_before)

        return "", ""



    def scroll_down(self):

        try:

            self.driver.execute_script(_SCROLL_JS)

        except WebDriverException as exc:

            log.debug("scroll failed: %s", exc)



    def has_more(self):

        """True while Flipkart's 'Hang on, loading content' sentinel is still on the page, i.e. the list

        has not really ended and a stalled load should be retried instead of accepted as the end."""

        try:

            return bool(self.driver.execute_script(

                "return [...document.querySelectorAll('div')].some(e=>e.children.length===0&&"

                "/loading content/i.test(e.textContent||''));"))

        except Exception:

            return False



    def clone(self):

        """A second, independent browser on the same reviews page (used to read several lists in parallel).

        It has its own Chrome profile (a profile folder cannot be shared by two running Chromes); the

        Flipkart cookies of this browser are copied over so a signed-in session carries across."""

        drv = make_driver(self.settings.get("headless", True), None, bool(self.settings.get("block_images", True)))

        pg = LivePage(drv, self.settings, self.diag_dir, self.selectors)

        pg.owns_driver = True

        try:

            try:

                cookies = self.driver.get_cookies()

                drv.get("https://www.flipkart.com/robots.txt")

                for c in cookies:

                    try:

                        drv.add_cookie({k: v for k, v in c.items() if k != "sameSite"})

                    except Exception:

                        pass

            except Exception:

                pass

            nav = pg.open_reviews(self.product_url)

            if not nav.get("reached"):

                raise RuntimeError("reviews page not reached: " + str(nav.get("reason"))[:80])

        except Exception:

            pg.close()

            raise

        return pg



    def close(self):

        if getattr(self, "owns_driver", False):

            try:

                self.driver.quit()

            except Exception:

                pass



    def _height(self):

        """Cheap page-height probe. Used so we do NOT re-parse the whole page source every 0.5 s."""

        try:

            return int(self.driver.execute_script("return document.body ? document.body.scrollHeight : 0") or 0)

        except WebDriverException:

            return 0



    def nudge_scroll(self):

        """Scroll UP a little, then back to the 'loading content' sentinel.

        Flipkart's loader fires when the sentinel ENTERS view. If it is already in view

        (e.g. a batch was slow), scrolling to the same spot again does nothing, so we

        move away and come back to re-trigger it."""

        try:

            self.driver.execute_script(_NUDGE_UP_JS)

            time.sleep(0.5)

            self.driver.execute_script(_SCROLL_JS)

        except WebDriverException as exc:

            log.debug("nudge scroll failed: %s", exc)



    def _scroll_top(self):

        try:

            self.driver.execute_script("window.scrollTo(0, 0);")

        except WebDriverException:

            pass



    def _chip_wait(self):

        """Seconds to keep looking for a chip before giving up (the row can render a moment late)."""

        return float(self.settings.get("chip_wait_seconds", 10))



    def _snapshot_once(self, tag):

        """Save HTML + screenshot of a failing page, once per tag per browser (see 'Diagnostics saved' in the log)."""

        if tag in self._snapped or not self.settings.get("snapshot_on_chip_failure", True):

            return

        self._snapped.add(tag)

        try:

            self.snapshot(tag)

        except Exception:

            pass



    def list_topics(self):

        """Topic chips on the reviews page, e.g. ['Overall', 'Sound Quality', 'Bass', ...]."""

        try:

            labels = self.driver.execute_script(_TOPICS_JS) or []

        except WebDriverException:

            return []

        return list(dict.fromkeys(l for l in labels if l))



    def _list_signature(self):

        """Fingerprint of the first few review cards (used to notice that a chip click swapped the list)."""

        try:

            d = parse_reviews_detailed(self.html(), self.selectors or {})

            return tuple((r["User Name"], r["Short Review"], r["Review Description"][:40])

                         for r in d["reviews"][:3])

        except Exception:

            return ()



    def _click_chip(self, label, group, strict=False, wait=None):

        """Click a chip in its own row, wait for the review list to change and settle.

        Returns True if the click landed. A list that looks identical afterwards is not an error

        (two sort orders can start with the same review).



        The chip row is looked for repeatedly for up to `chip_wait_seconds` (default 10): right after a

        page change it can be missing for a moment, and a single instant look used to fail the whole list."""

        before = self._list_signature()

        end = time.time() + (self._chip_wait() if wait is None else float(wait))

        clicked, scrolled = False, False

        while True:

            try:

                clicked = bool(self.driver.execute_script(_CLICK_CHIP_JS, label, list(group), bool(strict)))

            except WebDriverException as exc:

                log.warning("chip click '%s' failed: %s", label, str(exc)[:100])

                return False

            if clicked or time.time() >= end:

                break

            if not scrolled:                      # the chip row sits at the top of the page

                scrolled = True

                self._scroll_top()

            time.sleep(0.7)

        if not clicked:

            return False

        end = time.time() + 8

        while time.time() < end:                  # wait until the old list has been replaced

            time.sleep(0.5)

            now = self._list_signature()

            if now and now != before:

                break

        self._scroll_top()                        # restart the infinite-scroll sentinel

        self.wait_for_reviews(10)

        return True



    def select_topic(self, label):

        """Click a topic chip (Overall, Sound Quality, Bass, ...) and wait for its review list.



        First waits until the chip row really lists `label`; the click is then restricted to that row, so

        a stray piece of text with the same words (a review, a keyword chip) is never clicked by mistake.

        When the chip cannot be found, the chips that ARE on the page are logged and a screenshot is saved."""

        try:

            end = time.time() + self._chip_wait()

            topics, scrolled = [], False

            while True:

                topics = self.list_topics()

                if label in topics or time.time() >= end:

                    break

                if not scrolled:

                    scrolled = True

                    self._scroll_top()

                time.sleep(0.7)

            if label not in topics:

                log.info("topic chip '%s' not on the page; chip row shows: %s", label, topics[:12] or "nothing")

                self._snapshot_once(f"topic_{label}")

                return False

            if self._click_chip(label, topics, strict=True):

                return True

            self._snapshot_once(f"topic_{label}_click")

            return False

        except Exception as exc:

            log.warning("could not select topic '%s': %s", label, str(exc)[:100])

            return False



    def wait_for_change(self, prev_count, timeout, quiet=None):

        """Wait for the next batch of reviews after a scroll.



        Returns True as soon as new reviews appear. Returns False as soon as the page has been

        QUIET (height unchanged, even after re-nudging the scroll) for `quiet` seconds, which is how the

        end of a list shows itself - so finished lists are left in seconds instead of burning the full

        batch timeout. `timeout` is only the upper limit for a slow batch that is visibly still loading."""

        quiet = float(quiet if quiet is not None else self.settings.get("end_of_list_quiet_seconds", 6))

        timeout = max(timeout, quiet)

        start = time.time()

        end = start + timeout

        last_height = self._height()

        last_change = time.time()

        last_nudge = time.time()

        while time.time() < end:

            h = self._height()

            if h != last_height:

                last_height, last_change = h, time.time()

                if self.date_count() != prev_count:

                    time.sleep(0.8)  # let the batch finish rendering

                    return True

            if time.time() - last_change >= quiet:

                break                                   # nothing is loading: the end of this list

            if time.time() - last_nudge > 2.5:

                self.nudge_scroll()

                last_nudge = time.time()

            time.sleep(0.4)

        return self.date_count() != prev_count



    def click_latest(self):

        """Select the 'Latest' sort chip."""

        try:

            return self._click_chip("Latest", _SORT_GROUP)

        except Exception as exc:

            log.warning("could not click 'Latest': %s", str(exc)[:100])

            return False



    def select_sort_order(self, sort_order):

        """Select a sort sub-tab (Most Helpful / Latest / Positive / Negative) of the CURRENT topic.



        Returns True when the chip was clicked AND review cards are showing afterwards. The reason for

        a False is kept in self.last_page_navigation ('sort_control_not_found' means the chip row was

        missing; 'sort_selected_but_no_reviews' means the topic list is empty or could not be parsed)."""

        self.last_page_navigation = {"status": "sort_control_not_found", "review_blocks": 0}

        label = _SORT_LABELS.get(str(sort_order).strip().upper())

        if not label:

            return False

        try:

            if not self._click_chip(label, _SORT_GROUP):

                self._snapshot_once(f"sort_{label}")

                return False

            count = self.wait_for_review_entries(min(10, self.settings["page_wait_seconds"]))

            self.last_page_navigation = {

                "status": "loaded_with_reviews" if count else "sort_selected_but_no_reviews",

                "review_blocks": count,

            }

            if count <= 0:

                state, _ = classify_page(self.url, self.title, self.text(), 0)

                if state in ("blocked", "login_required", "not_found"):

                    self.last_page_navigation["status"] = state

                elif state == "empty":

                    self.last_page_navigation["status"] = "loaded_empty"

                return False

            log.info("Selected review sort %s through the page controls", sort_order)

            return True

        except Exception as exc:

            log.warning("could not select review sort %s: %s", sort_order, str(exc)[:100])

            return False



    def goto_page(self, url):

        """Navigate to a review page and record why it did/didn't yield review rows.



        The diagnostic intentionally stores no URL query, page text, or review data.

        """

        loaded = safe_get(self.driver, url, 1, self.settings["page_wait_seconds"])

        if not loaded:

            state, _ = classify_page(self.url, self.title, self.text(), 0)

            if state in ("blocked", "login_required", "not_found"):

                status = state

            else:

                status = "navigation_or_content_timeout"

            self.last_page_navigation = {"status": status, "review_blocks": 0}

            log.info("Pagination page probe: %s (review entries were not available)", status)

            return False



        count = self.wait_for_review_entries(self.settings["page_wait_seconds"])

        if count > 0:

            status = "loaded_with_reviews"

        else:

            state, _ = classify_page(self.url, self.title, self.text(), 0)

            status = {

                "blocked": "blocked",

                "login_required": "login_required",

                "not_found": "not_found",

                "empty": "loaded_empty",

            }.get(state, "loaded_without_review_entries")

        self.last_page_navigation = {"status": status, "review_blocks": count}

        log.info("Pagination page probe: %s (review entries=%d)", status, count)

        return count > 0



    def reload_reviews(self):

        """Reload the reviews page this browser started from, back to its default list, and wait until

        review cards AND the topic chip row are present again. Used by fk_collect to recover a browser whose

        page got into a bad state. Returns True when the page is usable. Never raises."""

        url = self.reviews_url or self.url

        if not url:

            return False

        try:

            if not safe_get(self.driver, url, 1, self.settings["page_wait_seconds"]):

                return False

            self.wait_for_reviews(self.settings["page_wait_seconds"])

            end = time.time() + self._chip_wait()

            while time.time() < end and not self.list_topics():

                time.sleep(0.5)

            return self.review_count() > 0

        except Exception as exc:

            log.warning("could not reload reviews page: %s", str(exc)[:100])

            return False



    def wait_for_reviews(self, timeout):

        """Wait until review dates appear and their count stabilizes."""

        end = time.time() + timeout

        previous_count = -1

        stable_checks = 0



        while time.time() < end:

            current_count = self.date_count()



            if current_count > 0 and current_count == previous_count:

                stable_checks += 1

                if stable_checks >= 3:

                    return current_count

            else:

                stable_checks = 0



            previous_count = current_count

            time.sleep(0.5)



        return self.date_count()



    def snapshot(self, tag):

        return dump_diagnostics(self.driver, self.diag_dir, tag)



    # -- navigation --------------------------------------------------------

    def open_reviews(self, product_url):

        """Open the reviews page. Returns {'reached','final_url','title','reason','reviews_in_dom'}."""

        self.product_url = product_url

        wait, retries = self.settings["page_wait_seconds"], self.settings["retries"]

        nav = {"reached": False, "final_url": "", "title": "", "reason": "", "reviews_in_dom": 0}



        def check(reason_prefix):

            n = self.wait_for_reviews(wait)

            nav.update(final_url=self.url, title=self.title, reviews_in_dom=n)

            on_reviews_url = "product-reviews" in nav["final_url"]

            summary_present = "ratings and" in self.text().lower()

            if on_reviews_url and (n > 0 or summary_present):

                nav["reached"] = True

                self.reviews_url = nav["final_url"]          # remembered for reload_reviews()

            elif not on_reviews_url:

                nav["reason"] = f"{reason_prefix}: redirected to {nav['final_url'][:90]}"

            else:

                nav["reason"] = f"{reason_prefix}: no review entries appeared within {wait}s"

            return nav["reached"]



        review_url = build_review_url(product_url)

        if review_url:

            if safe_get(self.driver, review_url, retries, wait):

                if check("direct reviews URL"):

                    return nav

            else:

                nav["reason"] = "direct reviews URL did not load"

        # Fallback: product page -> any link whose URL contains /product-reviews/

        if safe_get(self.driver, product_url, retries, wait):

            for a in self.driver.find_elements(By.CSS_SELECTOR, "a[href*='product-reviews']"):

                href = a.get_attribute("href")

                if href and safe_get(self.driver, href, retries, wait) and check("link from product page"):

                    return nav

            nav["reason"] = (nav["reason"] + "; " if nav["reason"] else "") + "no working reviews link on product page"

        else:

            nav["final_url"], nav["title"] = self.url, self.title

            nav["reason"] = (nav["reason"] + "; " if nav["reason"] else "") + "product page did not load"

        return nav





def collect_keyword_feedback(driver, review_url, selectors, settings, wait):

    """Best-effort keyword chips ('Sound', 'Battery' ...) -> positive/negative %. Never raises."""

    results = []

    try:

        labels = []

        for s in selectors.get("keyword_chip", []):

            try:

                labels += [e.text.strip() for e in driver.find_elements(By.CSS_SELECTOR, s) if e.text.strip()]

            except Exception:

                pass

        if not labels:  # tier 2: find the "...mention" heading and its short word-chips

            labels = driver.execute_script("""

                const leaves=[...document.querySelectorAll('body *')].filter(e=>e.children.length===0);

                const h=leaves.find(e=>/mention/i.test(e.textContent||'')&&(e.textContent||'').length<60);

                if(!h) return [];

                let node=h.parentElement;

                for(let i=0;i<5&&node;i++,node=node.parentElement){

                  const c=[...node.querySelectorAll('*')].filter(e=>e!==h&&e.children.length===0)

                    .map(e=>(e.textContent||'').trim())

                    .filter(t=>t.length>=3&&t.length<=30&&!/\\d/.test(t)&&!/mention/i.test(t));

                  if(c.length>=3) return [...new Set(c)];

                }

                return [];""") or []

        labels = list(dict.fromkeys(labels))[: settings["max_keyword_chips"]]

        for label in labels:

            try:

                if '"' in label:

                    continue

                if not safe_get(driver, review_url, 1, wait):

                    continue

                chips = _visible(driver, f'//*[normalize-space(text())="{label}"]')

                if not chips:

                    continue

                js_click(driver, chips[0])

                time.sleep(3)

                pos = neg = None

                for key, name in (("keyword_positive", "pos"), ("keyword_negative", "neg")):

                    for s in selectors.get(key, []):

                        try:

                            els = [e for e in driver.find_elements(By.CSS_SELECTOR, s) if e.is_displayed()]

                            if els:

                                val = els[0].text.strip()

                                pos, neg = (val, neg) if name == "pos" else (pos, val)

                                break

                        except Exception:

                            pass

                if pos is None or neg is None:

                    p2, n2 = parse_keyword_percentages(page_text(driver))

                    pos, neg = pos or p2, neg or n2

                if pos or neg:

                    results.append({"Keywords": label, "Positive Reviews": pos or "N/A",

                                    "Negative Reviews": neg or "N/A"})

            except Exception as exc:

                log.debug("keyword '%s' skipped: %s", label, exc)

    except Exception as exc:

        log.warning("Keyword feedback skipped: %s", exc)

    return results