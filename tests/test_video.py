from skystamp.video import clock, time_left


def test_clock():
    assert clock(5.9) == "0:05"
    assert clock(245) == "4:05"
    assert clock(3845) == "1:04:05"


def test_time_left_reads_like_a_person_would_say_it():
    assert time_left(4) == "almost done"
    assert time_left(44) == "about 40 s left"
    assert time_left(720) == "about 12 min left"
    assert time_left(3900) == "about 1 h 5 min left"
    assert time_left(7190) == "about 2 h left"
