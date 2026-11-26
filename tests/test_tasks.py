"""구매가 아닌 엄마 부탁(택시·숙소·병원 예약) → 할 일 → approve_task 승인서 → report_task 결과·엄마 문자."""

from oba_hack import pipeline, samples, store, tasks

from test_resume import CHILD, MOM, login, mcp


def call_with(c, sample_id):
    s = samples.get(sample_id)
    c.post("/api/simulate/call-ended", json={"transcript": s["transcript"]})
    return max(store.all_docs("call"), key=lambda x: x["received_at"])


def test_kind_and_connector():
    assert tasks.kind_of("목요일 아침에 정형외과 가는데 택시 좀 불러 줘") == "taxi"
    assert tasks.kind_of("13일에 하룻밤 묵을 숙소 좀 알아봐 줘") == "stay"
    assert tasks.kind_of("다음 주 화요일에 병원 예약도 좀 알아봐 줘") == "reservation"


def test_taxi_task_flow(client):
    c, _ = client
    fam = login(c, address="서울 마포구 월드컵로 1")
    call = call_with(c, "4-taxi")
    (t,) = [store.get("task", i) for i in call["task_ids"]]
    assert t["kind"] == "taxi" and t["status"] == "proposed" and t["address"] == "서울 마포구 월드컵로 1"
    digest = store.get("digest", call["id"])
    assert "Swing Taxi로 부를까요?" in digest["text"] and t["id"] in digest["text"]

    listed, early, ok, again, report, dup = mcp(
        fam["id"],
        ("list_parent_requests", {}),
        ("report_task", {"task_id": t["id"]}),
        ("approve_task", {"task_id": "택시"}),  # 이름으로도 찾는다
        ("approve_task", {"task_id": t["id"]}),
        ("report_task", {"task_id": t["id"], "success": True, "provider": "Swing Taxi", "amount": 12300,
                         "reference": "R-1", "details": "목 09:00 배차",
                         "message_to_mom": "엄마, 목요일 9시에 택시 와요~"}),
        ("report_task", {"task_id": t["id"], "success": True}),
    )
    assert listed["tasks"][0]["suggested_connector"] == "swing_taxi"
    assert early["ok"] is False and "approve_task" in early["message"]
    h = ok["handoff"]
    assert ok["ok"] and h["connector"] == "swing_taxi" and h["params"]["from"] == "서울 마포구 월드컵로 1"
    assert h["limits"]["confirm_with_user"] and h["limits"]["max_amount"] == 30000
    assert "Swing Taxi에 작업을 맡겨" in ok["message"] and "estimate_fare" in ok["message"]
    assert again["ok"]  # 승인을 다시 불러도 같은 승인서
    assert report["ok"] and report["task"]["status"] == "done"
    assert dup["ok"] and dup["already_recorded"]
    sms = [e for e in store.events() if e["kind"] == "sms_to_mom" and e["ref"] == t["id"]]
    assert [e["text"] for e in sms] == ["엄마, 목요일 9시에 택시 와요~"]


def test_stay_skip_and_resume_does_not_duplicate(client):
    c, _ = client
    fam = login(c)
    call = call_with(c, "5-stay")
    assert len(call["task_ids"]) >= 1
    pipeline.process_call(call["id"])  # 다시 처리해도 할 일은 그대로
    assert len([t for t in store.all_docs("task") if t["call_id"] == call["id"]]) == len(call["task_ids"])
    tid = call["task_ids"][0]
    ok, skipped, after = mcp(fam["id"],
                             ("approve_task", {"task_id": tid}),
                             ("reject_order", {"order_id": tid}),
                             ("approve_task", {"task_id": tid}))
    assert ok["handoff"]["connector"] == "nol" and ok["handoff"]["limits"]["max_amount"] == 100000
    assert "주소가 등록돼 있지 않아요" in ok["message"]
    assert skipped["ok"] and skipped["task"]["status"] == "skipped"
    assert after["ok"] is False and "하지 않기로" in after["message"]
