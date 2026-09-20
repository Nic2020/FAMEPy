# SPDX-License-Identifier: MIT
import numpy as np
import pytest
from canonical import scalar_object, series_object
from fake_native import FakeStatus

import famepy
from famepy import NameTruncatedError, do_write, listdb
from famepy._constants import FREQUENCY_MONTHLY, NAME_CAPACITY


@pytest.fixture
def populated(db):
    do_write(series_object("sales_a", "precision", FREQUENCY_MONTHLY, 0, np.zeros(2)), db)
    do_write(
        series_object("sales_b", "numeric", "quarterly_december", 0, np.zeros(2, np.float32)), db
    )
    do_write(scalar_object("sale", "precision", 1.0), db)
    do_write(scalar_object("other", "string", b"x"), db)
    do_write(scalar_object("flag", "boolean", 1), db)
    return db


def test_wildcard_patterns(populated):
    names = [info.name_text for info in listdb(populated)]
    assert names == ["FLAG", "OTHER", "SALE", "SALES_A", "SALES_B"]
    assert [i.name_text for i in listdb(populated, "sales?")] == ["SALES_A", "SALES_B"]
    assert [i.name_text for i in listdb(populated, "sale^")] == []
    assert [i.name_text for i in listdb(populated, "sales_^")] == ["SALES_A", "SALES_B"]
    assert famepy.is_wildcard("a?") and famepy.is_wildcard(b"a^") and not famepy.is_wildcard("ab")


def test_filters(populated):
    assert [i.name_text for i in listdb(populated, class_="scalar")] == [
        "FLAG",
        "OTHER",
        "SALE",
    ]
    assert [i.name_text for i in listdb(populated, type="precision,numeric")] == [
        "SALE",
        "SALES_A",
        "SALES_B",
    ]
    assert [i.name_text for i in listdb(populated, freq=["monthly"])] == ["SALES_A"]
    assert [i.name_text for i in listdb(populated, class_="series", type="NUMERIC")] == ["SALES_B"]
    with pytest.raises(ValueError):
        listdb(populated, class_="dropthis; ")
    with pytest.raises(ValueError):
        listdb(populated, type="unknown")


def test_scalars_use_quick_info_ranges(populated):
    info = next(i for i in listdb(populated) if i.name_text == "SALE")
    assert (info.first_index, info.last_index) == (0, 0)


def test_item_options_are_normalized_not_restored(populated):
    """Documented policy: listing leaves the four ITEM options ON."""
    fake = populated.session._native.fake
    fake.set_option(b"ITEM CLASS", b"OFF")
    fake.set_option(b"ITEM CLASS SCALAR", b"ON")
    names = [i.name_text for i in listdb(populated)]
    assert names == ["FLAG", "OTHER", "SALE", "SALES_A", "SALES_B"]
    assert fake.options[b"ITEM CLASS"] == b"ON" and b"ITEM CLASS SCALAR" not in fake.options
    listdb(populated, class_="scalar", alias=False)
    assert fake.options[b"ITEM CLASS"] == b"ON"
    assert fake.options[b"ITEM ALIAS"] == b"ON"
    assert fake.cursors == {}
    fake.fail_next["fame_quick_info"] = 999
    with pytest.raises(famepy.HLIError):
        listdb(populated)
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
    with pytest.raises(famepy.HLIError) as error:
        listdb(populated, type="numeric")
    assert error.value.status == 67
    assert fake.options[b"ITEM FREQUENCY"] == b"ON" and fake.options[b"ITEM ALIAS"] == b"ON"
    assert fake.cursors == {}
    # A body failure propagates even when a cleanup reset fails too.
    armed["on"] = True
    fake.fail_next["fame_init_wildcard"] = 909
    with pytest.raises(famepy.HLIError) as error:
        listdb(populated, type="numeric")
    assert error.value.status == 909 and fake.options[b"ITEM ALIAS"] == b"ON"


def test_long_names_and_truncation(db):
    long_name = "n" * NAME_CAPACITY
    do_write(scalar_object(long_name, "precision", 1.0), db)
    infos = listdb(db)
    assert infos[0].name_text == long_name.upper()
    with pytest.raises(NameTruncatedError) as error:
        listdb(db, capacity=10)
    assert error.value.returned_length == NAME_CAPACITY and error.value.capacity == 10
    assert db.session._native.fake.cursors == {}
    with pytest.raises(famepy.DataValidationError):
        listdb(db, capacity=0)


def test_unknown_status_propagates(db):
    fake = db.session._native.fake
    fake.fail_next["fame_init_wildcard"] = 909
    with pytest.raises(famepy.HLIError) as error:
        listdb(db, "")
    assert error.value.status == 909
