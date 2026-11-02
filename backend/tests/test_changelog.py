from app.changelog import ENTRIES, KINDS


def test_entries_are_valid_and_unique():
    keys = [e[0] for e in ENTRIES]
    assert len(keys) == len(set(keys))
    assert all(e[1] in KINDS and e[2] and e[3] for e in ENTRIES)


def test_api_lists_entries_newest_first(client, owner):
    r = client.get("/api/changelog", headers=owner)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == len(ENTRIES)
    assert rows[0]["released_on"] >= rows[-1]["released_on"]
    assert {"Добавили", "Убрали"} <= {x["kind_label"] for x in rows}
    assert client.get("/api/changelog").status_code == 401
