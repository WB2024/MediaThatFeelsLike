"""
Heuristic extraction of recommendations from comment text.

This is a candidate generator, not an oracle (see CLAUDE.md). Every candidate carries a
`method` and a `confidence`; the detail page defaults to including candidates at or above
INCLUDE_THRESHOLD and shows the rest collapsed for the user to promote.

Entry points:
    parse_comment(body, kind, comment_score=0, depth=0) -> list[Candidate]
    dedupe(candidates) -> list[Candidate]  (merges repeats across comments)
"""

import html
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

INCLUDE_THRESHOLD = 0.6
MAX_TITLE_WORDS = 12

# ---------------------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------------------

MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
BARE_URL = re.compile(r"(?<![(\[])https?://[^\s)\]>]+")
CODE_SPAN = re.compile(r"`[^`]*`")
BLOCKQUOTE = re.compile(r"^\s*>.*$", re.M)
EMPH = re.compile(r"(\*\*|__|\*|_)(?P<t>[^*_\n]{2,120}?)\1")
DQUOTE = re.compile(r"[\"“”](?P<t>[^\"“”\n]{2,120}?)[\"“”]")
YEAR_PAREN = re.compile(r"[\(\[]\s*(?P<y>(?:19\d{2}|20[0-2]\d))\s*[\)\]]")
TRAILING_YEAR = re.compile(r"[,\s]+(?P<y>(?:19\d{2}|20[0-2]\d))\s*$")
DASH = re.compile(r"\s+[-–—]\s+|\s*[–—]\s*")
LIST_PREFIX = re.compile(r"^\s*(?:[-*•▪‣]|\d{1,2}[.)])\s+")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“(*\[])|\s*;\s+|\s+\|\s+")
TITLECASE_RUN = re.compile(
    r"\b((?:[A-Z][\w'’]*|\d+)(?:\s+(?:[A-Z][\w'’]*|\d+|of|the|and|in|a|to|for|on|at|from|de|la|le|&))+)"
)
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿️]+")
NOISE_PARENS = re.compile(
    r"\s*[\(\[](?:official(?:\s+\w+)*|lyrics?|audio|music video|hd|4k|full album|album version|"
    r"remaster(?:ed)?(?:\s+\d{4})?|dir(?:ector|\.)?\s+[^)\]]+)[\)\]]",
    re.I,
)

LEADING_FILLER = re.compile(
    r"^(?:(?:oh|ooh|ohh|umm+|um|hmm+|yes|yeah|yep|yup|okay|ok|also|and|or|but|so|well|honestly|"
    r"literally|basically|obviously|definitely|probably|perhaps|maybe|possibly|surely|easily|"
    r"idk|imo|imho|tbh|lol|lmao|omg|hey|hi|dude|man|bro|op|personally|for me|i think|i'd say|"
    r"i would say|i'd go with|i'd go|i'd recommend|i recommend|i'd suggest|i suggest|i say|"
    r"i feel like|i vote|my vote is|my pick is|my pick|my answer is|my answer|gotta be|got to be|"
    r"has to be|it's gotta be|it has to be|it's|its|this is|that's|thats|this feels like|"
    r"this reminds me of|reminds me of|gives me|feels like|sounds like|looks like|screams|"
    r"vibes of|the vibe of|the vibes of|try|check out|check|watch|listen to|listen|see|go watch|"
    r"how about|what about|have you seen|have you heard|have you tried|ever seen|ever heard|"
    r"something like|anything like|anything by|anything from|everything by|the movie|the film|"
    r"the song|the track|the album|the soundtrack|the whole|the entire|the whole of|the entirety of|"
    r"pretty much|pretty much any|kind of|kinda|sort of|sorta|straight up|straight-up|100%|"
    r"first thing that came to mind|first thing i thought of|first thought|came to mind|"
    r"came to mind was|immediately thought of|immediately|instantly|instant|easy|easy one|"
    r"no question|without a doubt|hands down|for sure|absolutely|totally|big time|surprised no one said|"
    r"surprised nobody said|surprised no one has said|can't believe no one said|nobody said|"
    r"no one said|no one has said|somebody say|someone say|shoutout to|shout out to)"
    r"[\s,:;!.-]+)+",
    re.I,
)
TRAILING_FILLER = re.compile(
    r"(?:[\s,:;!.-]+(?:for sure|maybe|obviously|definitely|probably|perhaps|possibly|lol|lmao|imo|"
    r"imho|tbh|too|as well|also|though|tho|of course|comes to mind|came to mind|come to mind|"
    r"fits|fits this|fits perfectly|fits the vibe|is perfect|is a good one|is a great one|is the one|"
    r"is exactly this|is this|is this exactly|would fit|would work|works|is my pick|is my vote|"
    r"is the answer|is the move|is your answer|all the way|all day|hands down|100%|no question|"
    r"without a doubt|absolutely|totally|big time|for this|for this one|for the win|ftw|"
    r"the movie|the film|the song|the track|the album|the soundtrack|the ost|"
    r"perfectly|perfect|so well|really well|nicely|exactly|"
    r"by far|easily|instantly|immediately|obvs|obv|ngl|fr|frfr|for real|"
    r"ofc|duh|lowkey|highkey|honestly|literally|basically|i think|i guess|i suppose|i reckon|"
    r"in my opinion|in my humble opinion|is what i'd say|is what i'd pick|would be my pick|"
    r"is what came to mind|is what i thought of|is the first thing i thought of|"
    r"is the vibe|has this vibe|has these vibes|has this energy|has this feel|feels like this|"
    r"sounds like this|looks like this|is this energy|is this vibe|is this feeling|"
    r"gives me this|gives this|gives me these vibes|is what this feels like|is exactly what this feels like)"
    r")+[\s!.]*$",
    re.I,
)

# Whole-comment / list-item candidates starting like a sentence are almost never titles
# unless the rest is capitalised too ("It Follows" yes, "it follows the same vibe" no).
SENTENCE_START = re.compile(
    r"^(?:i|i'm|im|i've|ive|i'd|id|i'll|you|you're|your|we|we're|they|he|she|it|its|it's|this|"
    r"that|these|those|there|there's|here|what|why|how|when|who|where|which|is|are|was|were|"
    r"do|does|did|can|could|would|should|will|if|not|no|nope|nah|yes|yeah|so|and|but|or|"
    r"because|just|anyone|someone|everyone|nobody|somebody|thanks|thank|ty|same|agreed|"
    r"upvote|upvoted|lol|lmao|omg|wow|nice|good|great|love|loved|hate|op|edit|link|source|"
    r"song|movie|film|title|name|the|a|an|some|any|all|more|most|every|each|one|two|"
    r"literally|honestly|actually|basically|damn|dang|bruh|man|dude|please|pls|plz|remindme)\b",
    re.I,
)
JUNK_EXACT = {
    "this", "that", "same", "yes", "no", "lol", "thanks", "thank you", "agreed", "op", "upvote",
    "bump", "following", "good one", "great pick", "love this", "nice", "omg", "what", "why",
    "how", "wow", "reddit", "spotify", "youtube", "link", "edit", "source", "name", "title",
    "deleted", "removed", "unknown", "n/a", "none", "idk", "same here", "this one", "this guy",
    "this right here", "came here to say this", "came to say this", "beat me to it", "exactly",
    "perfect", "yes this", "this this this", "underrated", "classic", "masterpiece", "banger",
    "good call", "great call", "great choice", "good choice", "second this", "seconded", "thirded",
    "the soundtrack", "the score", "the album", "the song", "the movie", "the film", "the ost",
    "share", "spoiler", "spoilers", "sauce", "same energy", "mood", "vibes", "vibe", "this vibe",
    "yess", "yesss", "nope", "true", "facts", "fax", "real", "based", "cringe",
    "commenting", "saved", "saving", "remindme", "reminder", "great post",
    "good post", "nice post", "awesome", "amazing", "beautiful", "gorgeous", "stunning",
    "cool", "neat", "interesting", "haha", "hahaha", "lmfao", "rofl", "oof", "damn", "dang",
}
SIGNAL_HOSTS = {
    "open.spotify.com": ("music", 0.95),
    "spotify.link": ("music", 0.9),
    "music.apple.com": ("music", 0.95),
    "music.youtube.com": ("music", 0.9),
    "youtube.com": ("any", 0.85),
    "youtu.be": ("any", 0.85),
    "soundcloud.com": ("music", 0.9),
    "bandcamp.com": ("music", 0.9),
    "tidal.com": ("music", 0.9),
    "deezer.com": ("music", 0.9),
    "last.fm": ("music", 0.85),
    "genius.com": ("music", 0.85),
    "musicbrainz.org": ("music", 0.85),
    "discogs.com": ("music", 0.8),
    "imdb.com": ("movies", 0.95),
    "letterboxd.com": ("movies", 0.95),
    "themoviedb.org": ("movies", 0.95),
    "rottentomatoes.com": ("movies", 0.9),
    "justwatch.com": ("movies", 0.9),
    "wikipedia.org": ("any", 0.8),
}


@dataclass
class Candidate:
    title: str
    artist: str = ""
    year: int | None = None
    url: str = ""
    method: str = "short_comment"
    confidence: float = 0.5
    snippet: str = ""
    # filled in by the caller / dedupe
    comment: dict = field(default_factory=dict)
    mention_count: int = 1
    # Which half is the artist, and how sure: +1 the stored artist really is the artist
    # (a known name, an explicit "by", quotes around the title), -1 the comment had them
    # reversed and we swapped, 0 written order kept as a guess. See orient(); artist_title
    # candidates' values feed the per-comment consistency vote.
    orientation: int = 0

    @property
    def key(self):
        return normalise_key(self.artist, self.title)

    @property
    def is_valid(self):
        return bool(self.title) or bool(self.artist)


# ---------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------


def _norm_part(text):
    text = (text or "").lower()
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
    text = re.sub(r"\b(feat\.?|ft\.?|featuring)\b.*$", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return re.sub(r"^(the|a|an) ", "", text)


def normalise_key(artist, title):
    """Orientation-insensitive: "Sheryl Crow - All I Wanna Do" and "All I Wanna Do -
    Sheryl Crow" are the same recommendation whichever way round a commenter wrote it,
    so they must merge (and a stored swap must survive a re-parse)."""
    return " ".join(sorted(p for p in (_norm_part(artist), _norm_part(title)) if p))


def orient(first, second, known_artists):
    """Decide which half of an "X - Y" is the artist -- this sub's commenters write
    "Title - Artist" about as often as the reverse. Returns (artist, title, evidence):
    +1 the first half is a known artist (kept as written), -1 the second half is
    (swapped), 0 no idea (kept as written; a same-comment vote may flip it later)."""
    if not known_artists:
        return first, second, 0
    a = _norm_part(first) in known_artists
    b = _norm_part(second) in known_artists
    if a and not b:
        return first, second, 1
    if b and not a:
        return second, first, -1
    return first, second, 0


def _strip_wrappers(text):
    text = text.strip()
    text = text.strip("*_`~ \t")
    text = re.sub(r"^[\"'“”‘’(\[]+|[\"'“”‘’)\]]+$", "", text).strip()
    return text


def _word_count(text):
    return len(text.split())


def clean_title(text, kind):
    """Strip filler, punctuation and noise from a fragment. Returns (title, year)."""
    text = html.unescape(text)
    text = EMOJI.sub(" ", text)
    year = None
    m = YEAR_PAREN.search(text)
    if m:
        year = int(m.group("y"))
        text = (text[: m.start()] + " " + text[m.end():]).strip()
    text = _strip_wrappers(text)
    text = LEADING_FILLER.sub("", text)
    text = TRAILING_FILLER.sub("", text)
    text = _strip_wrappers(text)
    if year is None and kind == "movies":
        m = TRAILING_YEAR.search(text)
        if m:
            year = int(m.group("y"))
            text = text[: m.start()].strip()

    text = NOISE_PARENS.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" \t,.;:!-–—")
    text = _strip_wrappers(text)
    return text, year


CONNECTOR_WORDS = {"of", "the", "and", "in", "a", "to", "for", "on", "at", "from", "de", "la", "le", "&"}


def _trim_connectors(run):
    words = run.split()
    while words and words[-1].lower() in CONNECTOR_WORDS:
        words.pop()
    while words and words[0].lower() in CONNECTOR_WORDS - {"the", "a"}:
        words.pop(0)
    return " ".join(words)


def _looks_titlecase(text):
    words = [w for w in text.split() if w.lower() not in ("of", "the", "and", "in", "a", "to", "for", "on", "at", "from", "&")]
    if not words:
        return False
    caps = sum(1 for w in words if w[0].isupper() or w[0].isdigit())
    return caps / len(words) >= 0.6


def is_junk(title, method):
    t = title.lower().strip()
    if not t or len(t) < 2:
        return True
    if t in JUNK_EXACT:
        return True
    if "http" in t or "www." in t or "/" in t and " " not in t:
        return True
    if _word_count(t) > MAX_TITLE_WORDS:
        return True
    if t.endswith("?"):
        return True
    if re.fullmatch(r"[\W\d_]+", t):
        return True
    if method in ("short_comment", "list_item", "titlecase", "quoted"):
        if SENTENCE_START.match(title) and not _looks_titlecase(title):
            return True
        if _word_count(t) == 1 and not title[0].isupper() and not title[0].isdigit():
            return True
    return False


def split_artist_title(text, known_artists=frozenset(), lenient=False):
    """'X - Y' → (X, Y) when it looks like a song line at all; orient() decides which
    half is the artist. A first half that starts like a sentence ("I think - ...") is
    rejected unless the second half is a known artist -- then it's a title that just
    happens to start that way ("This Kiss - Faith Hill"). `lenient` skips that guard;
    parse_comment uses it to hold such lines back until the rest of the comment has
    shown it's written "Title - Artist"."""
    parts = DASH.split(text, maxsplit=1)
    if len(parts) != 2:
        return None
    first, second = parts[0].strip(), parts[1].strip()
    if not first or not second:
        return None
    if _word_count(first) > 7 or _word_count(second) > 10:
        return None
    if not lenient and first.lower().startswith(("i ", "it ", "this ", "that ")) and _norm_part(second) not in known_artists:
        return None
    return first, second


def split_title_by_artist(text):
    m = re.match(r"^(?P<title>.{1,90}?)\s+by\s+(?P<artist>.{1,60})$", text, re.I)
    if not m:
        return None
    title, artist = m.group("title").strip(), m.group("artist").strip()
    if _word_count(title) > 10 or _word_count(artist) > 7:
        return None
    return artist, title


def split_list(text, kind):
    """'A, B and C' → ['A', 'B', 'C'] when every part looks like a title."""
    if kind == "music" and "-" not in text and "–" not in text:
        parts = re.split(r"\s*,\s*|\s+and\s+|\s*&\s*|\s+or\s+", text)
    else:
        parts = re.split(r"\s*,\s*(?=[A-Z0-9\"“*])|\s+and\s+(?=[A-Z0-9\"“*])|\s*/\s*|\s+or\s+(?=[A-Z0-9])", text)
    parts = [p.strip(" .") for p in parts if p and p.strip(" .")]
    if len(parts) < 2:
        return [text]
    ok = all(0 < _word_count(p) <= 8 and (p[0].isupper() or p[0].isdigit() or p[0] in "\"“*") for p in parts)
    return parts if ok else [text]


def _title_from_url(url):
    """Derive a title from slug-style URLs where possible (letterboxd, tmdb, wikipedia)."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.").removeprefix("en.")
    path = parsed.path.rstrip("/")
    if host == "letterboxd.com" and "/film/" in path:
        slug = path.split("/film/")[1].split("/")[0]
        year = None
        m = re.search(r"-(19|20)\d{2}$", slug)
        if m:
            year = int(slug[m.start() + 1:])
            slug = slug[: m.start()]
        return slug.replace("-", " ").title(), year
    if host == "themoviedb.org" and "/movie/" in path:
        slug = path.split("/movie/")[1].split("/")[0]
        slug = re.sub(r"^\d+-?", "", slug)
        return slug.replace("-", " ").title() if slug else "", None
    if host.endswith("wikipedia.org") and "/wiki/" in path:
        slug = path.split("/wiki/")[1]
        slug = re.sub(r"_\((?:film|\d{4}_film|song|album|band)\)$", "", slug)
        return html.unescape(slug.replace("_", " ")), None
    return "", None


def _host_signal(url, kind):
    host = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    for known, (signal_kind, conf) in SIGNAL_HOSTS.items():
        if host == known or host.endswith("." + known):
            if signal_kind in ("any", kind):
                return conf
            return 0.5  # link to the wrong medium: still a title, less sure
    return 0.0


# ---------------------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------------------


def parse_comment(body, kind, comment_score=0, depth=0, known_artists=None):
    """Extract candidates from one comment body. `kind` is 'movies' or 'music'.
    `known_artists`: a set of normalised artist names (see vibes.models.KnownArtist) that
    lets "X - Y" be oriented; without it the written order is kept."""
    if not body or body.strip() in ("[deleted]", "[removed]"):
        return []
    text = html.unescape(body)
    text = BLOCKQUOTE.sub("", text)
    text = CODE_SPAN.sub(" ", text)
    known = known_artists or frozenset()

    candidates = []
    seen_spans = []
    deferred = []   # "X - Y" lines split_artist_title's sentence guard rejected; see below
    score_bonus = 0.1 if comment_score >= 10 else 0.05 if comment_score >= 3 else 0.0
    depth_bonus = 0.03 if depth == 0 else 0.0

    def add(title, artist="", year=None, url="", method="short_comment", confidence=0.5, snippet="", orientation=0):
        title, found_year = clean_title(title, kind)
        artist = _strip_wrappers(clean_title(artist, kind)[0]) if artist else ""
        year = year or found_year
        if kind == "music" and not artist and title:
            # A stray "Artist - Title" can hide inside any fragment.
            split = split_artist_title(title, known)
            if split:
                artist, title, orientation = orient(split[0], split[1], known)
                method = "artist_title" if method in ("short_comment", "list_item", "quoted", "titlecase") else method
                confidence = max(confidence, 0.85)
        if not title and not artist:
            return
        if title and is_junk(title, method):
            return
        if artist and is_junk(artist, "link"):
            return
        conf = min(0.99, confidence + score_bonus + depth_bonus)
        candidates.append(
            Candidate(title=title, artist=artist, year=year, url=url, method=method, confidence=round(conf, 2), snippet=snippet[:300], orientation=orientation)
        )

    # 1) Markdown links: the link text is usually the title.
    for m in MD_LINK.finditer(text):
        label, url = m.group(1).strip(), m.group(2)
        conf = _host_signal(url, kind) or 0.7
        if BARE_URL.fullmatch(label) or label.lower().startswith(("http", "www.", "link", "here", "this")):
            slug_title, slug_year = _title_from_url(url)
            if slug_title:
                add(slug_title, year=slug_year, url=url, method="link", confidence=conf, snippet=m.group(0))
            continue
        by = split_title_by_artist(label) if kind == "music" else None
        if by:
            add(by[1], artist=by[0], url=url, method="link", confidence=conf, snippet=m.group(0))
        else:
            add(label, url=url, method="link", confidence=conf, snippet=m.group(0))
        seen_spans.append(label)
    text = MD_LINK.sub(lambda m: m.group(1), text)

    # 2) Bare URLs: derive a title from the slug when possible, else remember the URL
    #    so the fragment around it can inherit it.
    bare_urls = BARE_URL.findall(text)
    for url in bare_urls:
        slug_title, slug_year = _title_from_url(url)
        if slug_title:
            add(slug_title, year=slug_year, url=url, method="link", confidence=_host_signal(url, kind) or 0.7, snippet=url)
    text = BARE_URL.sub(" ", text)
    inherited_url = bare_urls[0] if len(bare_urls) == 1 else ""

    # 3) Emphasised / quoted spans.
    for rx in (EMPH, DQUOTE):
        for m in rx.finditer(text):
            span = m.group("t").strip()
            if _word_count(span) > 10 or span in seen_spans:
                continue
            seen_spans.append(span)
            tail = text[m.end(): m.end() + 80]
            by = re.match(r"^\s*(?:by|from|-|–|—)\s+([A-Z][^.,;!\n]{1,60})", tail) if kind == "music" else None
            head = text[max(0, m.start() - 80): m.start()]
            before = re.search(r"([A-Z][^.,;!\n]{1,60}?)\s*(?:[-–—:]|'s)\s*$", head) if kind == "music" else None
            # Quotes/emphasis mark the title, so these are oriented by the markup itself.
            if by:
                add(span, artist=by.group(1), url=inherited_url, method="title_by_artist", confidence=0.85, snippet=m.group(0) + by.group(0), orientation=1)
            elif before:
                add(span, artist=before.group(1), url=inherited_url, method="artist_title", confidence=0.8, snippet=before.group(0) + m.group(0), orientation=1)
            else:
                add(span, url=inherited_url, method="quoted", confidence=0.75, snippet=m.group(0))
    plain = EMPH.sub(lambda m: m.group("t"), text)

    # 4) Line / list-item / sentence segments.
    lines = [ln for ln in plain.splitlines() if ln.strip()]
    short_lines = sum(1 for ln in lines if _word_count(LIST_PREFIX.sub("", ln)) <= 8)
    listy_comment = len(lines) >= 3 and short_lines / len(lines) >= 0.6
    whole_short = len(lines) == 1 and _word_count(plain) <= 9

    for ln in lines:
        is_item = bool(LIST_PREFIX.match(ln)) or listy_comment
        stripped = LIST_PREFIX.sub("", ln).strip()
        segments = [stripped] if is_item or whole_short else SENTENCE_SPLIT.split(stripped)
        for seg in segments:
            seg = seg.strip()
            if not seg or seg in seen_spans:
                continue
            _parse_segment(seg, kind, is_item, whole_short, inherited_url, add, seen_spans, known, deferred.append)

    # A commenter writes every "X - Y" line the same way round. If any line in this
    # comment could be oriented from a known artist, apply that to the lines that
    # couldn't -- "All I wanna do - Sheryl crow / This kiss - Faith Hill" flips as one.
    vote = sum(c.orientation for c in candidates if c.method == "artist_title")
    if vote:
        for c in candidates:
            if c.method == "artist_title" and c.orientation == 0 and c.artist and c.title:
                if vote < 0:
                    c.artist, c.title = c.title, c.artist
                c.orientation = 1 if vote > 0 else -1   # now oriented, just second-hand
    if vote < 0:
        # ...and lines whose first half read like a sentence ("This kiss - Faith Hill")
        # were held back; in a Title - Artist comment they're titles after all.
        for piece, (first, second), year, url in deferred:
            add(first, artist=second, year=year, url=url, method="artist_title", confidence=0.8, snippet=piece, orientation=-1)

    return _dedupe_within_comment([c for c in candidates if c.is_valid])


def _dedupe_within_comment(candidates):
    """The same title often surfaces via two methods in one comment (link text + the
    sentence around it); keep one, merging artist/year/url."""
    by_key = {}
    for cand in candidates:
        existing = by_key.get(cand.key)
        if existing is None:
            by_key[cand.key] = cand
            continue
        winner, loser = (cand, existing) if cand.confidence > existing.confidence else (existing, cand)
        winner.artist = winner.artist or loser.artist
        winner.year = winner.year or loser.year
        winner.url = winner.url or loser.url
        by_key[cand.key] = winner
    out = list(by_key.values())
    # "[Space Song](spotify) by Beach House" yields both a bare "Space Song" (link) and
    # "Beach House - Space Song"; fold the bare one into the richer one.
    with_artist = {}
    for c in out:
        if c.artist and c.title:
            # keyed by both halves: whichever way round the pair came out, a bare
            # mention of either half in the same comment is the same recommendation
            with_artist.setdefault(normalise_key("", c.title), c)
            with_artist.setdefault(normalise_key("", c.artist), c)
    folded = []
    for cand in out:
        target = with_artist.get(normalise_key("", cand.title)) if not cand.artist else None
        if target is not None and target is not cand:
            target.url = target.url or cand.url
            target.year = target.year or cand.year
            target.confidence = max(target.confidence, cand.confidence)
            continue
        folded.append(cand)
    return folded


def _parse_segment(seg, kind, is_item, whole_short, inherited_url, add, seen_spans, known=frozenset(), defer=None):
    body, seg_year = clean_title(seg, kind)
    if not body:
        return
    words = _word_count(body)

    # Movies: "Title (Year)" anywhere in the segment is a strong signal.
    if kind == "movies":
        found = False
        for m in YEAR_PAREN.finditer(seg):
            before = seg[: m.start()]
            before = re.split(r"[,;]|\band\b|\bor\b", before)[-1]
            title, _ = clean_title(before, kind)
            if title and (_word_count(title) > 8 or not _looks_titlecase(title)):
                run = [_trim_connectors(r) for r in TITLECASE_RUN.findall(title)]
                if run and _word_count(run[-1]) >= 1 and run[-1] != title:
                    title = run[-1]
            if title:
                add(title, year=int(m.group("y")), url=inherited_url, method="title_year", confidence=0.9, snippet=seg)
                found = True
        if found:
            return

    # Music: "Artist - Title" / "Title by Artist" patterns, possibly several per line.
    if kind == "music":
        pieces = split_list(body, kind) if ("-" in body or "–" in body) else [body]
        matched = False
        for piece in pieces:
            split = split_artist_title(piece, known)
            if split:
                artist, title, evidence = orient(split[0], split[1], known)
                add(title, artist=artist, year=seg_year, url=inherited_url, method="artist_title", confidence=0.85, snippet=piece, orientation=evidence)
                matched = True
                continue
            if defer is not None:
                held = split_artist_title(piece, known, lenient=True)
                if held:
                    defer((piece, held, seg_year, inherited_url))
            by = split_title_by_artist(piece)
            if by:
                add(by[1], artist=by[0], year=seg_year, url=inherited_url, method="title_by_artist", confidence=0.85, snippet=piece, orientation=1)  # "by" is explicit
                matched = True
        if matched:
            return
        m = re.match(r"^(?P<artist>[A-Z][^:]{1,50}):\s+(?P<title>[A-Z\"“].{1,80})$", body)
        if m:
            add(m.group("title"), artist=m.group("artist"), url=inherited_url, method="artist_title", confidence=0.7, snippet=seg)
            return

    # Short fragments: the whole thing is (probably) a title, maybe a comma list of them.
    if is_item or whole_short or words <= 6:
        base_conf = 0.65 if is_item else 0.6 if whole_short else 0.5
        parts = split_list(body, kind)
        if len(parts) > 1:
            base_conf = max(base_conf - 0.05, 0.5)
        for part in parts:
            if _word_count(part) == 1 and not part[0].isupper() and not part[0].isdigit():
                continue
            conf = base_conf
            if _word_count(part) == 1 and (len(part) < 4 or not whole_short):
                conf -= 0.05
            if _looks_titlecase(part) and _word_count(part) >= 2:
                conf += 0.05
            add(part, year=seg_year if len(parts) == 1 else None, url=inherited_url, method="list_item" if is_item else "short_comment", confidence=conf, snippet=seg)
        return

    # Long sentences: title-case runs are low-confidence hints, excluded by default.
    runs = [_trim_connectors(r) for r in TITLECASE_RUN.findall(seg)]
    for run in runs[:3]:
        if _word_count(run) < 2 or run in seen_spans:
            continue
        if seg.strip().startswith(run):
            # A sentence-initial run is usually just a capitalised sentence start...
            # unless it's followed by a clear "recommendation" verb.
            if not re.match(re.escape(run) + r"\s+(?:is|has|feels|fits|comes|gives|would|by)\b", seg.strip()):
                continue
        add(run, url=inherited_url, method="titlecase", confidence=0.35, snippet=seg)


def dedupe(candidates):
    """Merge repeated titles across comments: keep the best-scored instance, count the
    mentions and nudge confidence up for each extra independent mention."""
    merged = {}
    for cand in candidates:
        key = cand.key
        if not key:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = cand
            continue
        same_comment = cand.comment.get("id") and cand.comment.get("id") == existing.comment.get("id")
        if not same_comment:
            existing.mention_count += 1
        # Prefer the instance with richer metadata / higher confidence.
        # An oriented "X - Y" beats a guessed one: the key is orientation-insensitive, so
        # "All I Wanna Do - Sheryl Crow" and "Sheryl Crow - All I Wanna Do" land here
        # together and the version somebody could vouch for should win.
        richer = (bool(cand.artist), abs(cand.orientation), bool(cand.year), bool(cand.url), cand.confidence) > (
            bool(existing.artist), abs(existing.orientation), bool(existing.year), bool(existing.url), existing.confidence
        )
        winner, loser = (cand, existing) if richer else (existing, cand)
        winner.mention_count = existing.mention_count
        winner.url = winner.url or loser.url
        winner.year = winner.year or loser.year
        winner.artist = winner.artist or loser.artist
        merged[key] = winner
    out = list(merged.values())
    for cand in out:
        cand.confidence = round(min(0.99, cand.confidence + 0.05 * (cand.mention_count - 1)), 2)
    out.sort(key=lambda c: (-c.confidence, -c.mention_count, -(c.comment.get("score") or 0)))
    return out
