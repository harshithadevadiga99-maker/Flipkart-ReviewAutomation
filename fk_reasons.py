"""Consolidated Reason for a review - free, offline, rule based.

The review (short review + full description) is read clause by clause:

  1. the text is cleaned (typos such as 'huts', stretched words 'goooood', emojis, 'comfort.bass')
  2. it is split into clauses at . ! ? ; and at 'but / however / though / although / except / while'
  3. every product topic mentioned (Sound Quality, Bass, Mic / Call Quality, Comfort / Fit, ...) is found
     with the patterns in ASPECTS
  4. for every topic we work out whether it is PRAISED or a PROBLEM, from the words around it, with negation
     ("not comfortable" = problem, "no issues" = praise, "not bad" = praise)
  5. the label shown in the report depends on the sheet:

       Negative sheet : only the topics that are problems.  Nothing specific -> 'Generic Comments'
       Positive sheet : the topics the customer talks about.  Nothing specific -> 'Generic Comments'
                        (a problem mentioned inside a good review is added as '<topic> (issue)')

Everything the rules know lives in the tables below, so wording can be added without touching the logic.
Run  python fk_reasons.py "text of a review" [positive|negative]  to try one review, and
     python test_reasons.py  to run the regression cases."""
import re
import sys
import unicodedata
from bisect import bisect_right

GENERIC = "Generic Comments"
MARK_ISSUES_IN_POSITIVE = True      # positive sheet: 'Volume (issue)' for a problem raised inside a good review
ISSUE_SUFFIX = " (issue)"

# --------------------------------------------------------------------------- #
# Topics.  (label, is_problem_by_nature, regex).  ORDER MATTERS: a word matched by an earlier row is not
# matched again by a later one ('sound quality' belongs to Sound Quality, so Product Quality never sees it).
# is_problem_by_nature=True means that merely mentioning it is a complaint ('damaged', 'disconnects')
# unless it is negated ('no damage', 'never disconnects').
# --------------------------------------------------------------------------- #
_A = [
    # phrases that are only a generic complaint: consumed so that 'money' is not read as Value for Money
    (None, False, r"waste\s+of\s+(?:my\s+)?(?:money|time)|waste\s+your\s+(?:money|time)|money\s+waste|wasted?\s+(?:my\s+)?money"
                  r"|(?:don'?t|do\s+not|dont)\s+(?:buy|waste)(?:\s+your)?(?:\s+(?:money|time))?|not\s+recommend\w*|worst\s+product|total\s+waste|waste\s+product|paisa\s+barbad"),

    # app / hearable specific topics
    ("SKU Not supported", True, r"can'?t\s*connect.*\bapp\b|\bnot\s*(?:support(?:ed)?|show(?:n|ing)|found|recogni[sz]ed|compatible)\b.{0,15}\bapp\b"),
    ("In Ear Detection (Dev)", False, r"in[\s-]*ear\s*detection|ear\s*direction"),
    ("Auto Pause (dev)", False, r"\bauto.{0,10}pause\b|\bpause.{0,10}auto\b"),
    ("Phone becoming silent", True, r"\bsilent\b"),
    ("Vibration Mode", False, r"\bvib(?:e)?ration\s*mode\b|\bviberation\s*mode\b|\bvibrat(?:e|es|ing)\b"),
    ("EQ Customization", False, r"\b(?:eq|equali[sz]er)\b"),
    ("Login", False, r"\b(?:login|log\s*in|sign\s*in|otp)\b"),
    ("Location Permission", False, r"\b(?:location|gps|geolocation)\b"),
    ("Powering off (Device)", True, r"\b(?:auto\s*on\s*off|powering\s*off|power(?:s|ing)?\s*off|switch(?:es)?\s*off\s+(?:by\s+itself|automatically))\b"),
    ("Crash", True, r"\b(?:crash(?:es|ed|ing)?|hang(?:s|ing)?)\b"),
    ("Package box (Device)", False, r"\bopen\s*box\b|\bpackage\s*box\b"),
    ("Calibration", False, r"\bcalibration\b|\b3d\s*(?:audio|sound)\b"),

    ("Return / Replacement", False, r"\b(?:return\w*|refund\w*|replac\w*|warranty|customer\s*(?:care|service|support)|service\s*cent(?:er|re)|exchange\w*)\b"),

    # calls and microphone
    ("Mic / Call Quality", False, r"\bcalls?\s+(?:does\s*n'?t|do\s*n'?t|did\s*n'?t|won'?t|not|never)\s+(?:\w+\s+){0,2}(?:disconnect|end|cut)\w*|\bcalls?\s+(?:is\s+|are\s+)?not\s+(?:getting\s+)?(?:disconnect|end)\w*"),
    ("Mic / Call Quality", False, r"\b(?:mic|microphone|mike)s?(?:\s*(?:quality|clarity))?\b|\bcall(?:s|ing)?\s*(?:quality|sound|voice|clarity|recording)\b|\bcallers?\b"
                                  r"|\bvoice(?:\s*(?:quality|clarity|call))?\b(?!\s*assistant)|\bcall(?:s|ing)?\b|\btalk(?:ing)?\b"),

    # failures
    ("Durability", True, r"\b(?:within|in|after|only|just)\s+(?:a\s+|an\s+|one\s+|two\s+|three\s+|four\s+|five\s+|few\s+|couple\s+of\s+|\d+\s+)?"
                         r"(?:day|days|week|weeks|month|months|year|years)\b.{0,40}?\b(?:damag\w*|stopp?ed|dead|broke\w*|not\s+work\w*|fail\w*|problem|issue|faulty|defect\w*|stop\w*)\b"
                         r"|\b(?:damag\w*|stopp?ed|dead|broke\w*|not\s+work\w*|fail\w*|faulty|defect\w*)\b.{0,30}?\b(?:after|within|in)\s+"
                         r"(?:a\s+|an\s+|one\s+|two\s+|three\s+|few\s+|\d+\s+)?(?:day|days|week|weeks|month|months|year|years)\b"
                         r"|\bnot\s+last\w*\b|\blast(?:s|ed)?\s+(?:only|just)\b"),
    ("Defective / Damaged Product", True,
     r"\b(?:defect\w*|damag\w*|broke(?:n)?|faulty|dead|stopp?ed\s+work\w*|not\s+work\w*|(?:doesn'?t|does\s*not|didn'?t|did\s*not|isn'?t|won'?t)\s+(?:even\s+)?(?:work|function|start)\w*"
     r"|only\s+(?:from\s+)?one\s+(?:side|ear\w*|bud|earphone)|no\s+(?:sound|output)|not\s+(?:turn|switch)\w*\s+on|one\s+(?:side|ear\w*|bud|earphone)\b[^.,]{0,25}\b(?:not|stopp?ed|dead|no)\b)"),
    ("Fake / Not Original", True, r"\b(?:fake|duplicate|counterfeit|first\s*copy|not\s+(?:an?\s+)?(?:original|genuine)|local\s*(?:quality|product))\b"),
    ("Device disconnection", True, r"\bdisconnect\w*|\bkeeps?\s+(?:on\s+)?(?:cutting|dropping|breaking)\b|\bconnection\s+(?:drop\w*|lost|cut\w*|break\w*)\b|\bsignal\s+(?:drop|loss)\b"),
    ("Connectivity", True, r"\b(?:not|never|unable\s+to|fail\w*\s+to|can'?t|cannot)\s+(?:\w+\s+){0,2}(?:connect|pair)\w*"),

    ("Controls / Buttons", False, r"\bvolume\s*(?:up|down|control|button|key|rocker)s?\b|\bbuttons?\b|\bcontrols?\b|\bswitch(?:es)?\b|\bremote\b|\btouch\b|\bgestures?\b|\bpause\b|\bskip(?:ping)?\b|\bnext\s*track\b"),

    # sound
    ("Volume", False, r"\b(?:sound|awa+[jz])\s+(?:is\s+|was\s+|are\s+)?(?:very\s+|too\s+|so\s+|bahut\s+|a\s+bit\s+|a\s+little\s+|slightly\s+|bit\s+)?(?:low|less|slow|soft|kam)\b"),
    ("Bass", False, r"\b(?:sound\s+)?(?:bass(?:es|y)?(?:\s*quality)?|base(?=\s+(?:is|was|are|sound|boost|heavy|low|less)))\b|\bboomy\b|\bthump\w*\b"),
    ("Noise Cancellation", False, r"\b(?:noise\s*(?:cancel\w*|isolation|reduction)|anc|enc|isolation|ambient\s*(?:mode|sound|noise)|transparency)\b"),
    ("Sound Quality", False, r"\b(?:(?:sound|audio|music)\s*quality|sound|audio|music|songs?|clarity|treble|vocals?|crisp|distort\w*|crackl\w*|static|hiss\w*|noisy|noise|awa+[jz])\b"),
    ("Volume", False, r"\b(?:volume(?:\s*level)?|loud\w*)\b"),

    ("Battery", False, r"\b(?:battery(?:\s*life)?|backup|playback|play\s*time|playtime|standby|charg(?:e|es|ed|ing|er)|fast\s*charg\w*)\b"),
    ("Build Quality", False, r"\bcheap\s+(?:quality|plastic|build|material|looking)\b|\b(?:feels?|looks?)\s+cheap\b|\b(?:build(?:\s*quality)?|built|material|plastic|sturdy|sturdiness|flimsy|construction|finishing|finish)\b"),
    ("Comfort / Fit", False, r"\b(?:comfort\w*|discomfort|uncomfortable|fit(?:s|ting|ted)?|ear\s*(?:pain|ache|hurt\w*)|headache|hurts?|hurting|painful|pain|ergonomic|fall(?:s|ing)?\s*(?:out|off)|loose\s+(?:fit|fitting|in\s+(?:my\s+)?ears?)|ears?\s+loose|ear\s*tips?|eartips?|cushion\w*|snug|lightweight|light\s*weight)\b"),
    ("Wire / Cable", False, r"\b(?:wires?(?:\s*quality)?|cable|cord|tangl\w*)\b"),
    ("Jack / Connector", False, r"\b3\.5\s*mm\b|\b(?:jack|port|connector|plug\w*|unplug\w*|type[\s-]*c|usb[\s-]*c|aux|lightning)\b"),
    ("Design / Colour", False, r"\b(?:colou?rs?|design|looks?|looking|styl(?:e|ish)|appearance|sleek|shape|aesthetic\w*|attractive)\b"),
    ("Value for Money", False, r"\b(?:value(?:\s*for\s*(?:the\s*)?money)?|worth|paisa\s*vasool|budget|afford\w*|price[ds]?|pricing|cheap(?:er|est)?|cost\w*|pocket\s*friendly|money|deal|bargain|sasta)\b|₹\s*\d+|\brs\.?\s*\d+"),
    ("Packaging / Accessories", False, r"\b(?:packag\w*|packing|packed|box|pouch|bag|sealed|seal|accessor\w*|came\s+with|comes\s+with|(?:carry(?:ing)?|storage)\s*case)\b"),
    ("Delivery", False, r"\b(?:deliver\w*|courier|shipping|shipped|arrived|late|delay\w*|on\s*time)\b"),
    ("Connectivity", False, r"\b(?:connect(?:ion|ivity|ed|ing|s)?|pair(?:ing|ed|s)?|latency|lag(?:s|ging)?|bluetooth\s+range)\b"),
    ("Durability", False, r"\b(?:durab\w*|long\s*lasting|longevity)\b"),
    ("Product Quality", False, r"\bquality\b"),
]
ASPECTS = [(label, problem, re.compile(rx, re.I)) for label, problem, rx in _A]

# --------------------------------------------------------------------------- #
# Wording that says "good" or "bad".  Weight 2 = strong, 1 = normal.
# --------------------------------------------------------------------------- #
_POS = {2: "excellent amazing awesome superb fantastic outstanding perfect perfectly brilliant fabulous wonderful love loved loving "
           "best incredible terrific stunning mast zabardast shandar badiya crystal amazed",
        1: "good great nice clear crisp deep strong smooth comfortable worth satisfied happy impressed super better cool sturdy "
           "durable affordable light lightweight powerful rich balanced punchy solid acha accha achha recommended liked enjoy "
           "enjoyed enjoying genuine fast quick helpful perfect"}
_NEG = {2: "worst terrible horrible pathetic useless waste bad poor garbage trash bekar bakwas ghatiya faltu kharab disappointing "
           "disappointed disappoint awful rubbish junk",
        1: "low less slow weak feeble soft muffled dull flat noisy distorted hurts hurt hurting pain painful discomfort uncomfortable "
           "slippery loose issue issues problem problems difficulty unsatisfied dissatisfied lacking lack mediocre kam harsh scratchy "
           "annoying irritating heavy cheap short thin tangles tangled tangle brittle fragile drain drains draining late delayed headache"}
POS_WORDS = {w: k for k, ws in _POS.items() for w in ws.split()}
NEG_WORDS = {w: -k for k, ws in _NEG.items() for w in ws.split()}
NEG_WORDS.pop("heavy", None)            # 'heavy bass' is praise
NEG_WORDS.pop("cheap", None)            # 'cheap price' is praise; 'cheap quality' is handled as a phrase
WORD_SCORE = {**POS_WORDS, **NEG_WORDS}
INTENSIFIERS = {"very", "too", "so", "really", "extremely", "highly", "bahut", "totally", "absolutely"}
NEGATIONS = {"not", "no", "never", "without", "hardly", "cannot", "cant", "dont", "doesnt", "isnt", "wasnt", "didnt", "wont",
             "aint", "nor", "neither", "nothing", "nahi", "nahin"}

_PHRASES = [  # (regex, score) - read before single words; their words are not counted again
    (r"different\s+level|next\s+level|works?\s+(?:fine|well|perfectly|great|properly)|does\s+work|no\s+(?:issues?|problems?|complaints?|defects?|damage|lag)\b"
     r"|can'?t\s+complain|not\s+bad|worth\s+(?:it|the)|go\s+for\s+it|must\s+buy|paisa\s+vasool|love\s+it|value\s+for\s+(?:the\s+)?money|low\s+(?:price|budget|cost)|less\s+(?:price|cost|money)", 2),
    (r"expected\s+(?:more|better)|could\s+be\s+better|needs?\s+improvement|not\s+up\s+to|cheap\s+quality|looks?\s+cheap|feels?\s+cheap"
     r"|(?:doesn'?t|does\s*not|didn'?t|did\s*not|isn'?t|is\s*not|can'?t|cannot|won'?t|unable\s+to|fails?\s+to|not\s+able\s+to)\s+(?:\w+\s+){0,2}"
     r"(?:work\w*|connect\w*|disconnect\w*|transmit\w*|hear\w*|pick\w*|respond\w*|end\w*|cut\w*|stop\w*|come\w*|charge\w*|pair\w*|turn\w*|start\w*|play\w*|detect\w*|catch\w*|function\w*|capture\w*|record\w*)"
     r"|stopp?ed\s+working|no\s+(?:sound|output|voice|bass|response)|kaam\s+nahi", -2),
]
_PHRASES = [(re.compile(rx, re.I), sc) for rx, sc in _PHRASES]

_FUNCTIONAL = re.compile(r"not\s+work|stopp?ed\s+work|n't\s+(?:even\s+)?work|\bdead\b|no\s+(?:sound|output)", re.I)
_TOKEN = re.compile(r"[a-z0-9']+|,")
_CLAUSE_SPLIT = re.compile(r"[!?;\n]+|\.+(?=\s|$)|\s[—–-]\s|—|–|\b(?:but|however|though|although|whereas|except|while)\b")
_POS_EMOJI = "👍❤🥰😍🫶😊😀😁😄👌💯🔥⭐🤩😎💖💕♥"
_NEG_EMOJI = "👎😞😡😠😢😭😤🤬😒😕☹😖😣😩🤮"
_TYPOS = {"huts": "hurts", "becouse": "because", "qualty": "quality", "qulity": "quality", "bases": "bass", "baas": "bass",
          "earphones": "earphone", "ear phone": "earphone", "ear phones": "earphone", "soundquality": "sound quality",
          "bluetoth": "bluetooth", "excelent": "excellent", "awsome": "awesome", "mike": "mic"}
_TYPO_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(_TYPOS, key=len, reverse=True)) + r")\b")


def _normalise(text):
    t = unicodedata.normalize("NFKC", str(text or "")).lower()
    t = t.replace("’", "'").replace("‘", "'").replace("`", "'")
    for e in _POS_EMOJI:
        t = t.replace(e, " good ")
    for e in _NEG_EMOJI:
        t = t.replace(e, " bad ")
    t = re.sub(r"[\U00010000-\U0010ffff☀-➿️�]", " ", t)       # remaining emojis / broken characters
    t = re.sub(r"(?<=[a-z])\.(?=[a-z])", ". ", t)                                    # 'comfort.bass' -> 'comfort. bass'
    t = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", t)                                      # 'Value-for-money'
    t = re.sub(r"(.)\1{2,}", r"\1\1", t)                                             # 'goooood' -> 'good'
    t = _TYPO_RE.sub(lambda m: _TYPOS[m.group(1)], t)
    return re.sub(r"[ \t]+", " ", t).strip()


def _clauses(text):
    """[(offset_in_text, clause_text)] for non-empty clauses."""
    out, pos = [], 0
    for m in list(_CLAUSE_SPLIT.finditer(text)) + [None]:
        end = m.start() if m else len(text)
        piece = text[pos:end]
        if piece.strip():
            out.append((pos, piece.strip()))
        pos = m.end() if m else len(text)
    return out


def _negated_before(tokens, idx):
    """A negation word in the 3 tokens before tokens[idx] (not crossing a comma)."""
    for k in range(idx - 1, max(idx - 4, -1), -1):
        t = tokens[k][0]
        if t == ",":
            return False
        if t in NEGATIONS or t.endswith("n't"):
            nxt = tokens[k + 1][0] if k + 1 < len(tokens) else ""
            return nxt != "only"
    return False


def _sentiments(clause, tokens, starts):
    """[(token_index, score)] - phrases first, then single words with negation."""
    hits, used = [], set()
    for rx, score in _PHRASES:
        for m in rx.finditer(clause):
            first = bisect_right(starts, m.start()) - 1
            last = bisect_right(starts, m.end() - 1) - 1
            w = float(score)
            if w > 0 and _negated_before(tokens, first):
                w = -w * 0.9
            hits.append((first, w))
            used.update(range(first, last + 1))
    for k, (tok, _s, _e) in enumerate(tokens):
        if k in used or tok not in WORD_SCORE:
            continue
        w = float(WORD_SCORE[tok])
        if k and tokens[k - 1][0] in INTENSIFIERS:
            w *= 1.5
        if _negated_before(tokens, k):
            w = -w * 0.9
        hits.append((k, w))
    return hits


def _read_clause(clause, offset):
    """[(label, position_in_text, polarity)] for one clause. polarity: +1 praised, -1 problem, 0 unclear."""
    tokens = [(m.group(), m.start(), m.end()) for m in _TOKEN.finditer(clause)]
    starts = [t[1] for t in tokens]
    commas = [k for k, t in enumerate(tokens) if t[0] == ","]

    def seg(k):
        return bisect_right(commas, k - 1) if commas else 0

    mentions, taken = [], []
    for label, problem, rx in ASPECTS:
        for m in rx.finditer(clause):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            if label:
                mentions.append((label, problem, m, bisect_right(starts, m.start()) - 1))
    if not mentions:
        return []
    sent = _sentiments(clause, tokens, starts)
    seg_has_topic = {seg(i) for *_x, i in mentions}
    out = []
    for label, problem, m, i in mentions:
        score = 0.0
        for k, w in sent:
            d = abs(k - i)
            if d > 8:
                continue
            wt = 1.0 / (1.0 + 0.4 * d)
            if seg(k) != seg(i) and seg(k) in seg_has_topic:
                wt *= 0.1                       # the wording belongs to another topic in the same clause
            score += w * wt
        if problem:                              # 'damaged', 'disconnects': a problem unless clearly negated/praised
            absent = _negated_before(tokens, i) and not re.match(r"(?:not|no|never|n't)", m.group(), re.I)
            pol = 1 if (absent or score >= 1.0) else -1
        else:
            pol = 1 if score >= 0.3 else (-1 if score <= -0.3 else 0)
        out.append((label, offset + m.start(), pol, problem, m.group()))
    # 'mic is not working' is a Mic problem, not a separate 'Defective Product'
    topic_problem = any(pol < 0 and not prob for _l, _p, pol, prob, _t in out)
    return [(l, p, pol) for l, p, pol, _prob, txt in out
            if not (l == "Defective / Damaged Product" and topic_problem and _FUNCTIONAL.search(txt))]


def classify_review(text, sentiment=None):
    """Return (reason_detail, consolidated_reason).

    sentiment: 'Positive' / 'Negative' (the sheet the review belongs to) or None.
    reason_detail lists every topic found with what was said about it, for checking;
    consolidated_reason is the label for the report."""
    norm = _normalise(text)
    found = {}                                               # label -> [first_position, [polarities]]
    for off, clause in _clauses(norm):
        for label, pos, pol in _read_clause(clause, off):
            slot = found.setdefault(label, [pos, []])
            slot[0] = min(slot[0], pos)
            slot[1].append(pol)
    ordered = sorted(found.items(), key=lambda kv: kv[1][0])
    verdict = {}
    for label, (_pos, pols) in ordered:
        verdict[label] = -1 if -1 in pols else (1 if 1 in pols else 0)
    detail = ", ".join(f"{l} ({'problem' if v < 0 else 'praised' if v > 0 else 'mentioned'})" for l, v in verdict.items()) or GENERIC

    problems = [l for l, v in verdict.items() if v < 0]
    others = [l for l, v in verdict.items() if v >= 0]
    sent = (sentiment or "").lower()
    if sent == "negative":
        labels = problems or [l for l, v in verdict.items() if v == 0]
    elif sent == "positive":
        labels = others + ([l + ISSUE_SUFFIX if MARK_ISSUES_IN_POSITIVE else l for l in problems])
    else:
        labels = list(verdict)
    return detail, (", ".join(labels) if labels else GENERIC)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    d, c = classify_review(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print("detail      :", d)
    print("consolidated:", c)