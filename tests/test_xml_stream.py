import pytest

from astroskysim.indi.xml_stream import XmlStreamError, XmlStreamSplitter, parse_element


def test_self_closing_at_top_level():
    s = XmlStreamSplitter()
    assert s.feed('<getProperties version="1.7"/>') == ['<getProperties version="1.7"/>']


def test_element_split_across_feeds():
    s = XmlStreamSplitter()
    assert s.feed('<defTextVector device="M"><oneText name="a">x') == []
    assert s.feed("</oneText></defTextVector>") == [
        '<defTextVector device="M"><oneText name="a">x</oneText></defTextVector>'
    ]


def test_gt_and_slash_inside_attribute_value():
    """A splitter that adjusts depth on any '>' or '/' desynchronises here:
    both are legal inside an attribute value. Guard for that bug class."""
    s = XmlStreamSplitter()
    got = s.feed(
        '<newTextVector device="Scope &gt; 2" name="a/b">'
        '<oneText name="t">v</oneText></newTextVector>'
    )
    assert len(got) == 1
    el = parse_element(got[0])
    assert el.device == "Scope > 2"
    assert el.attrib["name"] == "a/b"
    assert el.child_values() == {"t": "v"}


def test_multiple_elements_and_interstitial_whitespace():
    s = XmlStreamSplitter()
    assert s.feed("  <a/>\n  <b><c/></b>  ") == ["<a/>", "<b><c/></b>"]


def test_self_closing_children_do_not_close_parent_early():
    s = XmlStreamSplitter()
    got = s.feed(
        '<defSwitchVector><oneSwitch name="A"/><oneSwitch name="B"/></defSwitchVector>'
    )
    assert len(got) == 1
    assert [c.name for c in parse_element(got[0]).children] == ["A", "B"]


def test_byte_at_a_time_matches_one_shot():
    stream = '<x a="1"><y>hi</y></x><z/>'
    s = XmlStreamSplitter()
    acc = []
    for ch in stream:
        acc += s.feed(ch)
    assert acc == ['<x a="1"><y>hi</y></x>', "<z/>"]


def test_oversized_element_is_refused():
    s = XmlStreamSplitter(max_element_bytes=32)
    with pytest.raises(XmlStreamError, match="exceeded"):
        s.feed("<a>" + "x" * 100 + "</a>")


def test_splitter_recovers_after_oversized_element():
    s = XmlStreamSplitter(max_element_bytes=32)
    with pytest.raises(XmlStreamError):
        s.feed("<a>" + "x" * 100 + "</a>")
    assert s.feed("<b/>") == ["<b/>"]


def test_malformed_element_raises():
    with pytest.raises(XmlStreamError, match="malformed"):
        parse_element("<a><b></a>")
