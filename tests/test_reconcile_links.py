"""The link reconciler decides whether to move a foreign key, so its matcher must not
merge two different firms, and must not split one firm written down two ways.

An earlier version keyed off "company_name disagrees with the linked company". On live
data that flagged 213 people and would have unlinked 182 who never moved - "Midi" vs
"Midi Health", "SignalFire" vs "SignalFire - Full-time". These tests pin the rules that
replaced it.
"""
import reconcile_links as rl


def co(name, aliases=None):
    return {"name": name, "aliases": aliases}


# --- variants of one company must NOT count as a move ---

def test_trailing_words_are_the_same_company():
    assert rl.same_company("Midi Health", co("Midi"))
    assert rl.same_company("SignalFire", co("SignalFire · Full-time"))
    assert rl.same_company("Furey (www.fureyfs.com)", co("Furey"))
    assert rl.same_company("Kinetic", co("Kinetic.Auto"))


def test_legal_suffixes_and_punctuation_are_ignored():
    assert rl.same_company("Acme, Inc.", co("Acme LLC"))
    assert rl.same_company("The Condor Co", co("Condor"))


def test_alias_on_the_company_row_matches():
    push = co("PushPress", ["PushPress - The Gym Operating System"])
    assert rl.same_company("PushPress - The Gym Operating System", push)


# --- genuinely different companies must count as a move ---

def test_different_companies_do_not_match():
    assert not rl.same_company("Randstad USA", co("Tandem PV"))
    assert not rl.same_company("GitLab", co("MARI Group"))


def test_a_shared_middle_word_is_not_a_match():
    """Prefix means variant; a word in common does not."""
    assert not rl.same_company("Bay Capital", co("Sierra Capital"))
    assert not rl.same_company("Health Midi", co("Midi"))


def test_empty_text_never_matches():
    assert not rl.same_company("", co("Midi"))
    assert not rl.same_company(None, co("Midi"))


# --- placeholders are never a way in ---

def test_placeholders_are_recognised():
    for name in ["Stealth", "Stealth Startup", "Stealth AI Startup",
                 "Various Startups", "Self Employed", "Freelance", "Retired", "Unknown"]:
        assert rl.is_placeholder(name), name


def test_real_companies_are_not_placeholders():
    for name in ["PushPress", "Drata", "Tandem PV", "Consultancy Group"]:
        assert not rl.is_placeholder(name), name
