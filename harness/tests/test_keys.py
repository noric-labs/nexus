from nexus_harness.keys import parse_keys


def test_plain_text():
    assert parse_keys("open mailbox") == ([("lit", "open mailbox")], 12)


def test_named_keys_and_literals():
    segs, n = parse_keys("look<Enter><Esc>j<Down><pgdn><F12><C-c><m-x><C-M-q>")
    assert segs == [("lit", "look"), ("key", "Enter"), ("key", "Escape"), ("lit", "j"),
                    ("key", "Down"), ("key", "NPage"), ("key", "F12"), ("key", "C-c"),
                    ("key", "M-x"), ("key", "C-M-q")]
    assert n == 4 + 1 + 1 + 1 + 6


def test_lt_unknown_and_newlines():
    assert parse_keys("a<lt>b") == ([("lit", "a<b")], 3)
    assert parse_keys("<Foo>") == ([("lit", "<Foo>")], 5)  # unknown tag is typed
    assert parse_keys("<a b>") == ([("lit", "<a b>")], 5)
    assert parse_keys("go\r\nnorth\n") == (
        [("lit", "go"), ("key", "Enter"), ("lit", "north"), ("key", "Enter")], 10)  # \r counts, as in game.ts
    assert parse_keys("<Space><BS><Tab>") == ([("key", "Space"), ("key", "BSpace"), ("key", "Tab")], 3)
    assert parse_keys("") == ([], 0)
