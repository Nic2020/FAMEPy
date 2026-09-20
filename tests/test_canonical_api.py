# SPDX-License-Identifier: MIT
"""The canonical FAME.jl-spelled API, exercised as real workflows.

Every test here uses only the package-level names: ``opendb``, ``workdb``,
``postdb``, ``closedb``, ``quick_info``, ``listdb``, ``do_read``,
``do_write``, ``refame``, ``unfame``, ``readfame``, ``writefame``, ``fame``,
``init_chli`` and ``close_chli``. The workflows follow the reference's
argument order and cover object-first reads and writes, metadata refresh,
posting ownership, stale handles, the missing/empty/text policies and the
package's refusal to touch a destination before its input is valid.
"""

import math

import numpy as np
import pytest
from fake_native import SENTINELS, make_fake
from tsecon import MIT, TSeries, Unit, Workspace, mm, qq

import famepy
from famepy import (
    FameDatabase,
    FameObject,
    HLIError,
    bridge,
    closedb,
    do_read,
    do_write,
    listdb,
    opendb,
    postdb,
    quick_info,
    readfame,
    refame,
    unfame,
    workdb,
    writefame,
)

REFERENCE_EXPORTS = {
    "version",
    "check_status",
    "FameDatabase",
    "fame",
    "workdb",
    "opendb",
    "postdb",
    "closedb",
    "FameObject",
    "quick_info",
    "listdb",
    "do_read",
    "do_write",
    "readfame",
    "unfame",
    "writefame",
    "refame",
}
REFERENCE_QUALIFIED = {"init_chli", "close_chli", "HLIError", "FameRange", "Period"}
RETIRED = {
    "Database",
    "open_database",
    "work_database",
    "read_object",
    "write_object",
    "list_objects",
    "run_command",
    "initialize",
    "finalize",
    "ObjectInfo",
    "RawScalar",
    "RawSeries",
    "RangeSpec",
    "FameError",
    "scalar",
    "series",
}
RETIRED_BRIDGE = {
    "read_value",
    "write_value",
    "read_tseries",
    "write_tseries",
    "read_scalar",
    "write_scalar",
    "read_workspace",
    "write_workspace",
    "read_workspace_report",
    "write_workspace_report",
    "to_fame",
    "from_fame",
    "from_tseries",
    "to_tseries",
    "raw_kind",
}


# -- the surface ---------------------------------------------------------------


def test_reference_names_are_the_public_surface():
    exported = set(famepy.__all__)
    assert REFERENCE_EXPORTS <= exported and REFERENCE_QUALIFIED <= exported
    assert not RETIRED & exported
    for name in RETIRED:
        assert not hasattr(famepy, name), name
    for name in RETIRED_BRIDGE:
        assert not hasattr(bridge, name), name
    assert {"readfame_report", "writefame_report", "NameList", "DateSeries"} <= set(bridge.__all__)
    assert famepy.__version__ == "0.1.0rc2"
    # The reference's ``!`` names and keyword are the only spelling adaptations.
    assert famepy.closedb.__name__ == "closedb" and famepy.do_read.__name__ == "do_read"
    assert "class_" in famepy.listdb.__code__.co_varnames


# -- named objects -----------------------------------------------------------------


def test_object_first_write_read_and_convert(db):
    ts = TSeries(mm(2024, 1), [100.0, np.nan, 110.0])
    obj = refame("sales", ts, database=db)
    assert isinstance(obj, FameObject)
    assert (obj.name, obj.is_series, obj.kind, obj.frequency_label) == (
        b"sales",
        True,
        "precision",
        "monthly",
    )
    assert obj.first_index == bridge.mit_to_index(mm(2024, 1), database=db)
    assert obj.last_index == obj.first_index + 2
    do_write(obj, db)
    listed = listdb(db)
    assert [o.name_text for o in listed] == ["SALES"] and listed[0].data is None
    loaded = do_read(quick_info(db, "sales"), db)
    assert loaded.data is not obj.data and loaded.data.dtype == np.float64
    assert famepy.classify_by_sentinel(loaded.data, "precision", db.session.sentinels).tolist() == [
        0,
        1,
        0,
    ]
    back = unfame(loaded, database=db)
    assert back.firstdate == ts.firstdate and np.array_equal(back.values, ts.values, equal_nan=True)
    with pytest.raises(bridge.MissingValueError):
        unfame(loaded, database=db, missing="strict")
    # The listing entry is the same kind of object: do_read fills it in place.
    entry = listed[0]
    assert do_read(entry, db) is entry and entry.data is not None
    assert str(entry) == f"SALES: series,precision,monthly,{obj.first_index}:{obj.last_index}"
    assert "SALES" in repr(entry) and "3 values" in repr(entry)


@pytest.mark.parametrize(
    "item", [1.25, np.float32(2.5), True, "sample", "{a,b}", ["a", "b"], qq(2021, 2)]
)
def test_every_kind_round_trips_object_first(db, item):
    obj = refame("sample", item, database=db)
    do_write(obj, db)
    loaded = do_read(quick_info(db, "sample"), db)
    assert (loaded.kind, loaded.type_code, loaded.class_code) == (
        obj.kind,
        obj.type_code,
        obj.class_code,
    )
    actual = unfame(loaded, database=db)
    expected = unfame(obj, database=db)
    if isinstance(expected, bridge.StringSeries):
        assert actual.values == expected.values and actual.firstdate == expected.firstdate
    else:
        assert actual == expected and type(actual) is type(expected)


def test_do_read_uses_the_object_range_and_refreshes_metadata(db):
    first = bridge.mit_to_index(mm(2020, 1), database=db)
    do_write(refame("s", TSeries(mm(2020, 1), np.arange(6.0)), database=db), db)
    # A caller of the reference sets the range on the object before reading.
    obj = FameObject("s", "series", "precision", "monthly", first + 2, first + 3)
    assert do_read(obj, db).data.tolist() == [2.0, 3.0]
    assert (obj.first_index, obj.last_index) == (first + 2, first + 3)
    obj.last_index = first + 4
    assert do_read(obj, db).data.tolist() == [2.0, 3.0, 4.0]
    obj.first_index = None
    assert do_read(obj, db).data.tolist() == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert obj.first_index == first
    for first_index, last_index in ((first - 1, None), (None, first + 6), (first + 3, first + 2)):
        bad = FameObject("s", "series", "precision", "monthly", first_index, last_index)
        with pytest.raises(famepy.DataValidationError):
            do_read(bad, db)
        assert bad.data is None
    # NC endpoints mean "unset", as an object described while empty would carry.
    unset = FameObject(
        "s", "series", "precision", "monthly", SENTINELS.index_nc, SENTINELS.index_nc
    )
    assert len(do_read(unset, db).data) == 6
    # The read is refused when the stored object no longer matches the description.
    stale = quick_info(db, "s")
    do_write(refame("s", 5.0, database=db), db, replace=True)
    with pytest.raises(famepy.DataValidationError, match="quick_info"):
        do_read(stale, db)
    assert stale.data is None
    fresh = do_read(quick_info(db, "s"), db)
    assert fresh.is_scalar and unfame(fresh, database=db) == 5.0
    with pytest.raises(famepy.DataValidationError):
        do_read(FameObject("s", "scalar", "numeric", "undefined"), db)


def test_empty_and_reference_empty_series(db):
    do_write(refame("e", TSeries(mm(2020, 3), np.empty(0)), database=db), db)
    obj = do_read(quick_info(db, "e"), db)
    assert obj.is_empty(SENTINELS.index_nc) and len(obj.data) == 0
    assert obj.first_index == obj.last_index == SENTINELS.index_nc
    with pytest.raises(bridge.EmptySeriesError):
        unfame(obj, database=db)
    assert len(unfame(obj, database=db, empty_firstdate=mm(2020, 3))) == 0
    with pytest.raises(famepy.DataValidationError):
        do_read(FameObject("e", "series", "precision", "monthly", 5, 6), db)
    do_write(refame("r", TSeries(mm(2020, 3), np.empty(0)), database=db, empty="reference"), db)
    stored = do_read(quick_info(db, "r"), db)
    assert len(stored.data) == 1
    assert len(unfame(stored, database=db, empty="reference")) == 0
    assert len(unfame(stored, database=db)) == 1


def test_text_policies_through_unfame(db):
    do_write(refame("t", "caf\xe9", database=db, text="utf-8"), db)
    loaded = do_read(quick_info(db, "t"), db)
    assert loaded.data == b"caf\xc3\xa9"
    assert unfame(loaded, database=db, text="utf-8") == "caf\xe9"
    assert unfame(loaded, database=db, text="bytes") == b"caf\xc3\xa9"
    with pytest.raises(famepy.TextEncodingError):
        unfame(loaded, database=db)
    with pytest.raises(famepy.TextEncodingError):
        refame("t", "caf\xe9", database=db)
    with pytest.raises(ValueError):
        unfame(loaded, database=db, text="latin-1")


def test_do_write_replace_and_failure_before_mutation(db):
    fake = db.session._native.fake
    do_write(refame("x", 1.0, database=db), db)
    with pytest.raises(HLIError):
        do_write(refame("x", 2.0, database=db), db, replace=False)
    assert unfame(do_read(quick_info(db, "x"), db), database=db) == 1.0
    do_write(refame("x", 2.0, database=db), db)
    assert unfame(do_read(quick_info(db, "x"), db), database=db) == 2.0
    fake.calls.clear()
    broken = refame("x", 3.0, database=db)
    broken.data = "not a number"
    with pytest.raises(famepy.DataValidationError):
        do_write(broken, db)
    with pytest.raises(famepy.DataValidationError):
        do_write(FameObject("x", "series", "precision", "monthly", 1, 5, np.zeros(2)), db)
    with pytest.raises(ValueError):
        do_write(refame("x", 3.0, database=db), db, replace=True, basis=999)
    with pytest.raises(TypeError):
        do_write(3.0, db)
    assert fake.calls == []
    assert unfame(do_read(quick_info(db, "x"), db), database=db) == 2.0


def test_listing_and_other_classes(db):
    from fake_native import FakeObject

    do_write(refame("a", 1.0, database=db), db)
    do_write(refame("b", TSeries(qq(2020, 1), [1.0]), database=db), db)
    db.session._native.fake.handles[db.key].objects["F"] = FakeObject(3, 5, 0, 0, 0, 1, 0, [1.0])
    names = [o.name_text for o in listdb(db)]
    assert names == ["A", "B", "F"]
    assert [o.name_text for o in listdb(db, class_="series")] == ["B"]
    assert [
        o.name_text for o in listdb(db, type="precision,numeric", freq="quarterly_december")
    ] == ["B"]
    assert [o.name_text for o in listdb(db, freq="undefined")] == ["A", "F"]
    with pytest.raises(ValueError):
        listdb(db, freq="quarterly")
    formula = quick_info(db, "f")
    assert formula.object_class is famepy.ObjectClass.FORMULA
    with pytest.raises(famepy.UnsupportedOperationError):
        do_read(formula, db)
    with pytest.raises(famepy.UnsupportedOperationError):
        do_write(FameObject("g", "formula", "precision", "undefined", data=1.0), db)


# -- workspaces ----------------------------------------------------------------------


def test_workspace_round_trip_through_a_path(session, tmp_path):
    path = tmp_path / "reference.db"
    fake = session._native.fake
    w = Workspace(a=1.0, b=TSeries(qq(2020, 1), [1.0, np.nan]), c=Workspace(n=Workspace(s="Hello")))
    with pytest.raises(ValueError):
        writefame(path, w)  # a path needs an explicit mode
    assert not path.exists()
    assert writefame(path, w, mode="create") == ("a", "b", "c_n_s")
    assert "cfmpodb" in fake.calls and session.open_databases == ()
    assert [o.name_text for o in listdb(path)] == ["A", "B", "C_N_S"]
    back = readfame(path)
    assert list(back) == ["a", "b", "c_n_s"] and back.a == 1.0 and back.c_n_s == "Hello"
    nested = readfame(path, collect=[("c", ["n"])])
    assert nested.c.n.s == "Hello"
    assert list(readfame(path, "b", "a")) == ["b", "a"]
    assert list(readfame(path, "?", class_="scalar")) == ["a", "c_n_s"]
    assert list(readfame(path, "?", type="precision", freq="quarterly_december")) == ["b"]
    assert list(readfame(path, "?", freq="")) == list(back)
    with pytest.raises(bridge.MissingValueError):
        readfame(path, "b", missing="strict")
    report = bridge.readfame_report(path, missing="strict")
    assert [str(f) for f in report.failures] == ["b: MissingValueError"]
    assert session.open_databases == ()


def test_workspace_handle_ownership_and_tuple_argument(session, tmp_path):
    path = tmp_path / "handle.db"
    fake = session._native.fake
    db = opendb(path, "create", session=session)
    writefame(db, (Workspace(a=1.0), Workspace(b=2.0)))
    writefame(db, {"c": 3.0}, prefix="in")
    assert "cfmpodb" not in fake.calls
    assert set(readfame(db)) == {"a", "b", "in_c"}
    assert closedb(db) is db and not db.is_open
    with pytest.raises(famepy.StaleHandleError):
        readfame(db)
    with opendb(path, session=session) as reopened:
        assert isinstance(reopened, FameDatabase)
        assert readfame(reopened) == Workspace()  # never posted: discarded by the fake
    assert not reopened.is_open
    db = opendb(path, "update", session=session)
    writefame(db, Workspace(kept=1.0))
    postdb(db)
    closedb(db)
    assert readfame(path) == Workspace(kept=1.0)


def test_invalid_workspace_never_opens_the_destination(session, tmp_path):
    path = tmp_path / "kept.db"
    writefame(path, Workspace(kept=42.0), mode="create")
    before = path.read_bytes()
    fake = session._native.fake
    for data, options in (
        ({"x": object()}, {"mode": "overwrite"}),
        ({"x": 2**53 + 1}, {"mode": "overwrite"}),
        ({"x": MIT(Unit(), 1)}, {"mode": "overwrite"}),
        ({"x": "caf\xe9"}, {"mode": "overwrite"}),
        ({"x": 1.0, "X": 2.0}, {"mode": "overwrite"}),
        ({"x": 1.0}, {"mode": "rewrite"}),
        ({"x": 1.0}, {"mode": "overwrite", "text": "latin-1"}),
        ({"x": 1.0}, {"mode": "overwrite", "observed": 999}),
    ):
        fake.calls.clear()
        with pytest.raises((ValueError, TypeError)):
            writefame(path, data, **options)
        assert fake.calls == [] and path.read_bytes() == before
    report = bridge.writefame_report(path, {"x": object()}, mode="overwrite")
    assert report.written == () and fake.calls == [] and path.read_bytes() == before
    assert readfame(path) == Workspace(kept=42.0)


# -- runtime, work database and commands ------------------------------------------------


def test_lifecycle_work_database_and_commands(fake, tmp_path):
    session = famepy.Session(native=fake).initialize()
    assert famepy.current_session() is session and famepy.version() == 11.8
    work = workdb()
    assert work.is_work and workdb() is work
    do_write(refame("w", 3.0), work)
    assert unfame(do_read(quick_info(work, "w"), work)) == 3.0
    assert famepy.fame("display 2+2", temp_dir=tmp_path) == b"4\n"
    assert famepy.fame("display 2+2", quiet=True, temp_dir=tmp_path) == b""
    with pytest.raises(famepy.CommandError) as info:
        famepy.fame("fail 513", temp_dir=tmp_path)
    assert info.value.status == 513 and isinstance(info.value, HLIError)
    famepy.close_chli()
    assert session.state == "finalized" and not work.is_open
    with pytest.raises(famepy.StaleHandleError):
        quick_info(work, "w")
    assert closedb(work) is work
    with pytest.raises(famepy.RuntimeStateError, match="spawned process"):
        famepy.init_chli()
    with pytest.raises(famepy.RuntimeStateError):
        workdb()
    famepy.close_chli()  # harmless after finalization
    assert fake.fake.calls.count("cfmfin") == 1


def test_object_model_validation():
    obj = FameObject("x", "series", "monthly", "case", 1, 2, np.array([5, 6], dtype=np.int64))
    assert (obj.kind, obj.date_frequency, obj.frequency_label) == ("date", 129, "case")
    assert obj.type_label == "date:monthly" and obj.length(SENTINELS.index_nc) == 2
    assert obj.range(SENTINELS.index_nc) == famepy.FameRange(232, 1, 2)
    obj.name = b"renamed"
    assert obj.name_text == "renamed"
    obj.frequency = famepy.FREQUENCIES["monthly"]
    assert obj.frequency_label == "monthly"
    with pytest.raises(famepy.DataValidationError, match="case frequency"):
        FameObject("d", "scalar", "case", "undefined", data=5)
    with pytest.raises(famepy.DataValidationError):
        obj.first_index = 2**63
    with pytest.raises(famepy.DataValidationError):
        obj.last_index = 1.5
    with pytest.raises(ValueError):
        FameObject("x", "nothing", "precision", "undefined")
    with pytest.raises(ValueError):
        FameObject("x", "scalar", "precision", "no such frequency")
    with pytest.raises(famepy.TextEncodingError):
        FameObject("caf\xe9", "scalar", "precision", "undefined")
    assert (
        str(FameObject("q", "scalar", "precision", "undefined"))
        == "q: scalar,precision,undefined,:"
    )
    scalar = FameObject("q", "scalar", "precision", "undefined", data=math.nan)
    assert "scalar data" in repr(scalar) and scalar.has_data


def test_refame_needs_a_session_only_for_calendar_conversions(fake):
    with pytest.raises(famepy.RuntimeStateError):
        refame("x", 1.0)
    session = famepy.Session(native=make_fake(persist=False)).initialize()
    try:
        assert refame("x", 1.0, session=session).data == 1.0
        obj = refame("q", qq(2020, 1), session=session)
        assert obj.kind == "date" and obj.date_frequency == 162
        assert unfame(obj, session=session) == qq(2020, 1)
    finally:
        session.finalize()
