# SPDX-License-Identifier: MIT
import numpy as np
import pytest
from fake_native import FakeStatus

import famepy
from famepy import NameTruncatedError, list_objects, scalar, series, write_object
from famepy._constants import FREQUENCY_MONTHLY, NAME_CAPACITY


@pytest.fixture
def populated(db):
    write_object(db, "sales_a", series("precision", FREQUENCY_MONTHLY, 0, np.zeros(2)))
    write_object(db, "sales_b", series("numeric", "quarterly_december", 0, np.zeros(2, np.float32)))
    write_object(db, "sale", scalar("precision", 1.0))
    write_object(db, "other", scalar("string", b"x"))
    write_object(db, "flag", scalar("boolean", 1))
    return db


def test_wildcard_patterns(populated):
    names = [info.name_text for info in list_objects(populated)]
    assert names == ["FLAG", "OTHER", "SALE", "SALES_A", "SALES_B"]
    assert [i.name_text for i in list_objects(populated, "sales?")] == ["SALES_A", "SALES_B"]
    assert [i.name_text for i in list_objects(populated, "sale^")] == []
    assert [i.name_text for i in list_objects(populated, "sales_^")] == ["SALES_A", "SALES_B"]
    assert famepy.is_wildcard("a?") and famepy.is_wildcard(b"a^") and not famepy.is_wildcard("ab")


def test_filters(populated):
    assert [i.name_text for i in list_objects(populated, classes="scalar")] == [
        "FLAG",
        "OTHER",
        "SALE",
    ]
    assert [i.name_text for i in list_objects(populated, types="precision,numeric")] == [
        "SALE",
        "SALES_A",
        "SALES_B",
    ]
    assert [i.name_text for i in list_objects(populated, frequencies=["monthly"])] == ["SALES_A"]
    assert [i.name_text for i in list_objects(populated, classes="series", types="NUMERIC")] == [
        "SALES_B"
    ]
    with pytest.raises(ValueError):
        list_objects(populated, classes="dropthis; ")
    with pytest.raises(ValueError):
        list_objects(populated, types="unknown")


def test_scalars_use_quick_info_ranges(populated):
    info = next(i for i in list_objects(populated) if i.name_text == "SALE")
    assert (info.first_index, info.last_index) == (0, 0)


def test_item_options_are_normalized_not_restored(populated):
    """Documented policy: listing leaves the four ITEM options ON."""
    fake = populated.session._native.fake
    fake.set_option(b"ITEM CLASS", b"OFF")
    fake.set_option(b"ITEM CLASS SCALAR", b"ON")
    names = [i.name_text for i in list_objects(populated)]
    assert names == ["FLAG", "OTHER", "SALE", "SALES_A", "SALES_B"]
    assert fake.options[b"ITEM CLASS"] == b"ON" and b"ITEM CLASS SCALAR" not in fake.options
    list_objects(populated, classes="scalar", alias=False)
    assert fake.options[b"ITEM CLASS"] == b"ON"
    assert fake.options[b"ITEM ALIAS"] == b"ON"
    assert fake.cursors == {}
    fake.fail_next["fame_quick_info"] = 999
    with pytest.raises(famepy.FameError):
        list_objects(populated)
    assert fake.cursors == {}
    assert fake.options[b"ITEM TYPE"] == b"ON"


def test_cleanup_attempts_every_reset_after_a_failure(populated, monkeypatch):
    fake = populated.session._native.fake
    original = fake.set_option
    armed = {"on": True}

    def flaky(name, value):
        if armed["on"] and name == b"ITEM TYPE" and value == b"ON":
            armed["on"] = False
            fake.options[b"ITEM FREQUENCY"] = b"OFF"
            fake.options[b"ITEM ALIAS"] = b"OFF"
            raise FakeStatus(67)
        return original(name, value)

    monkeypatch.setattr(fake, "set_option", flaky)
    with pytest.raises(famepy.FameError) as error:
        list_objects(populated, types="numeric")
    assert error.value.status == 67
    assert fake.options[b"ITEM FREQUENCY"] == b"ON" and fake.options[b"ITEM ALIAS"] == b"ON"
    assert fake.cursors == {}
    # A body failure propagates even when a cleanup reset fails too.
    armed["on"] = True
    fake.fail_next["fame_init_wildcard"] = 909
    with pytest.raises(famepy.FameError) as error:
        list_objects(populated, types="numeric")
    assert error.value.status == 909 and fake.options[b"ITEM ALIAS"] == b"ON"


def test_long_names_and_truncation(db):
    long_name = "n" * NAME_CAPACITY
    write_object(db, long_name, scalar("precision", 1.0))
    infos = list_objects(db)
    assert infos[0].name_text == long_name.upper()
    with pytest.raises(NameTruncatedError) as error:
        list_objects(db, capacity=10)
    assert error.value.returned_length == NAME_CAPACITY and error.value.capacity == 10
    assert db.session._native.fake.cursors == {}
    with pytest.raises(famepy.DataValidationError):
        list_objects(db, capacity=0)


def test_unknown_status_propagates(db):
    fake = db.session._native.fake
    fake.fail_next["fame_init_wildcard"] = 909
    with pytest.raises(famepy.FameError) as error:
        list_objects(db, "")
    assert error.value.status == 909
