import pytest

from reddit_sync import parser


def titles(cands):
    return [(c.artist, c.title, c.year) for c in cands]


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Mazzy Star - Fade Into You", [("Mazzy Star", "Fade Into You", None)]),
        ("Cigarettes After Sex – Apocalypse", [("Cigarettes After Sex", "Apocalypse", None)]),
        ("Fade Into You by Mazzy Star", [("Mazzy Star", "Fade Into You", None)]),
        ("Definitely Beach House - Space Song, for sure", [("Beach House", "Space Song", None)]),
        ("[Space Song](https://open.spotify.com/track/abc) by Beach House", [("Beach House", "Space Song", None)]),
        ("Try *Nightcall* by Kavinsky", [("Kavinsky", "Nightcall", None)]),
        ("The Cure - Pictures of You (1989)", [("The Cure", "Pictures of You", 1989)]),
        ("Radiohead - Nude, Radiohead - Reckoner and Portishead - Roads", [("Radiohead", "Nude", None), ("Radiohead", "Reckoner", None), ("Portishead", "Roads", None)]),
    ],
)
def test_music_patterns(body, expected):
    cands = parser.parse_comment(body, "music")
    assert titles(cands) == expected


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Drive (2011)", [("", "Drive", 2011)]),
        ("Lost in Translation", [("", "Lost in Translation", None)]),
        ("Blade Runner 2049 for sure", [("", "Blade Runner 2049", None)]),
        ("Maybe The Lighthouse (2019) or The Witch (2015)", [("", "The Lighthouse", 2019), ("", "The Witch", 2015)]),
        ("I think Nightcrawler fits this perfectly", [("", "Nightcrawler", None)]),
        ("- Heat\n- Collateral\n- Thief", [("", "Heat", None), ("", "Collateral", None), ("", "Thief", None)]),
        ("Drive, Nightcrawler, and Collateral", [("", "Drive", None), ("", "Nightcrawler", None), ("", "Collateral", None)]),
        ("https://letterboxd.com/film/under-the-silver-lake/", [("", "Under The Silver Lake", None)]),
        ("It Follows", [("", "It Follows", None)]),
    ],
)
def test_movie_patterns(body, expected):
    cands = parser.parse_comment(body, "movies")
    assert titles(cands) == expected


@pytest.mark.parametrize(
    "body",
    [
        "this",
        "Came here to say this",
        "what movie is this from?",
        "lol same",
        "I love this sub so much, everyone here has great taste and it makes my day",
        "[deleted]",
        "Thanks!",
    ],
)
def test_junk_yields_nothing(body):
    assert parser.parse_comment(body, "movies") == []
    assert parser.parse_comment(body, "music") == []


def test_long_sentence_gives_low_confidence_titlecase_hint():
    body = "The whole aesthetic of this really reminds me of Blade Runner and also Ghost in the Shell to be honest"
    cands = parser.parse_comment(body, "movies")
    assert {c.title for c in cands} >= {"Blade Runner", "Ghost in the Shell"}
    assert all(c.confidence < parser.INCLUDE_THRESHOLD for c in cands)


def test_dedupe_merges_mentions_and_boosts():
    a = parser.parse_comment("Drive (2011)", "movies", comment_score=1, depth=1)
    b = parser.parse_comment("drive", "movies")
    c = parser.parse_comment("Drive", "movies", comment_score=1)
    for i, group in enumerate((a, b, c)):
        for cand in group:
            cand.comment = {"id": f"c{i}", "score": 1}
    merged = parser.dedupe(a + b + c)
    assert len(merged) == 1
    assert merged[0].year == 2011
    assert merged[0].mention_count == 2  # "drive" (lowercase junk) never became a candidate
    assert merged[0].confidence == round(min(0.99, 0.9 + 0.05), 2)


def test_score_and_depth_bonus():
    low = parser.parse_comment("Nightcrawler", "movies", comment_score=0, depth=2)[0]
    high = parser.parse_comment("Nightcrawler", "movies", comment_score=25, depth=0)[0]
    assert high.confidence > low.confidence


# -- artist/title orientation -----------------------------------------------------------

REVERSED = "All I wanna do - Sheryl crow\nThis kiss - faith hill\nBreathe - Michelle branch"


def test_without_known_artists_the_written_order_is_kept():
    # ...and the sentence-looking "This kiss - ..." line is dropped rather than guessed.
    assert titles(parser.parse_comment(REVERSED, "music")) == [("All I wanna do", "Sheryl crow", None), ("Breathe", "Michelle branch", None)]


def test_one_known_artist_orients_the_whole_comment_and_rescues_the_held_back_line():
    cands = parser.parse_comment(REVERSED, "music", known_artists={"sheryl crow"})
    assert sorted(titles(cands)) == [("Michelle branch", "Breathe", None), ("Sheryl crow", "All I wanna do", None), ("faith hill", "This kiss", None)]
    assert all(c.method == "artist_title" and c.orientation == -1 for c in cands)


def test_known_artist_on_the_left_confirms_the_written_order():
    cands = parser.parse_comment("Sheryl Crow - All I Wanna Do, Faith Hill - This Kiss", "music", known_artists={"sheryl crow"})
    assert titles(cands) == [("Sheryl Crow", "All I Wanna Do", None), ("Faith Hill", "This Kiss", None)]
    assert [c.orientation for c in cands] == [1, 1]


def test_both_halves_known_means_no_evidence():
    assert parser.orient("Heart", "Genesis", {"heart", "genesis"}) == ("Heart", "Genesis", 0)
    assert parser.orient("Genesis", "Grimes", {"genesis"}) == ("Genesis", "Grimes", 1)


def test_normalise_key_ignores_orientation_articles_and_features():
    assert parser.normalise_key("Sheryl Crow", "All I Wanna Do") == parser.normalise_key("All I Wanna Do", "Sheryl Crow")
    assert parser.normalise_key("The Cure", "Pictures of You") == parser.normalise_key("Cure", "Pictures Of You (Remastered)")
    assert parser.normalise_key("Beach House", "Space Song") == parser.normalise_key("Space Song feat. Nobody", "Beach House")


def test_dedupe_merges_both_spellings_and_keeps_the_oriented_one():
    reversed_ = parser.parse_comment("All I wanna do - Sheryl crow", "music")[0]
    reversed_.comment = {"id": "c1"}
    oriented = parser.parse_comment("Sheryl Crow - All I Wanna Do", "music", known_artists={"sheryl crow"})[0]
    oriented.comment = {"id": "c2"}
    merged = parser.dedupe([reversed_, oriented])
    assert len(merged) == 1
    assert (merged[0].artist, merged[0].title, merged[0].mention_count) == ("Sheryl Crow", "All I Wanna Do", 2)
