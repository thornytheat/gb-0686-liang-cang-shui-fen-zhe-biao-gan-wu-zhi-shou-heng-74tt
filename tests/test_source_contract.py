"""来源契约：字段级合并、可追溯事实、切换原子性与续跑。"""
from decimal import Decimal as D

import pytest

from app import ledger, store
from app.pipeline import run
from app.source_contract import merge, resolve, source_of

SCALE = """id,bin,gross,tare,unit,version,source,trip
S001,甲仓,1200,200,kg,1,地磅,T001
"""
LAB = """id,moisture,impurity,version,source,trip
L001,18,2,2,化验,T001
"""
SCALE2 = """id,bin,gross,tare,unit,version,source,trip
S001,甲仓,1200,200,kg,1,地磅,T001
S002,乙仓,1300,300,kg,1,地磅,T002
"""


def test_source_detection_by_fields():
    assert source_of({'id': '1', 'gross': '1200', 'tare': '200', 'unit': 'kg'}) == 'scale'
    assert source_of({'id': '1', 'moisture': '18', 'impurity': '2'}) == 'lab'
    assert source_of({'id': '1', 'gross': '1200', 'tare': '200', 'unit': 'kg',
                      'moisture': '18', 'impurity': '2'}) == 'full'


def test_field_level_merge_keeps_both_sides():
    # 整行取最高版本会丢掉地磅重量；锁首版会丢掉复验水杂。
    rows = [
        {'id': 'S001', 'bin': '甲仓', 'gross': '1200', 'tare': '200', 'unit': 'kg',
         'version': '1', 'source': '地磅', 'trip': 'T001', '_line': 2},
        {'id': 'L001', 'moisture': '18', 'impurity': '2',
         'version': '2', 'source': '化验', 'trip': 'T001', '_line': 3},
    ]
    facts, bad = merge(rows)
    assert not bad
    fact = facts[0]
    assert fact['id'] == 'S001'            # 原磅单身份不因复验而改变
    assert fact['net_kg'] == D('1000')      # 重量来自地磅
    assert fact['moisture'] == D('18')      # 水杂来自复验
    assert fact['impurity'] == D('2')
    assert fact['evidence']['gross']['source'] == 'scale'
    assert fact['evidence']['moisture']['source'] == 'lab'
    assert fact['evidence']['moisture']['line'] == 3


def test_retest_overrides_only_its_own_fields():
    rows = [
        {'id': 'S001', 'bin': '甲仓', 'gross': '1200', 'tare': '200', 'unit': 'kg',
         'version': '1', 'source': '地磅', 'trip': 'T001', '_line': 2},
        {'id': 'L001', 'moisture': '18', 'impurity': '2',
         'version': '2', 'source': '化验', 'trip': 'T001', '_line': 3},
        {'id': 'L002', 'moisture': '17', 'impurity': '1',
         'version': '3', 'source': '化验', 'trip': 'T001', '_line': 4},
    ]
    fact = merge(rows)[0][0]
    assert (fact['moisture'], fact['impurity']) == (D('17'), D('1'))
    assert fact['evidence']['moisture']['version'] == 3
    assert fact['evidence']['gross']['version'] == 1


def test_same_version_field_conflict_isolates_whole_trip():
    rows = [
        {'id': 'S001', 'bin': '甲仓', 'gross': '1200', 'tare': '200', 'unit': 'kg',
         'version': '1', 'source': '地磅', 'trip': 'T001', '_line': 2},
        {'id': 'L001', 'moisture': '18', 'impurity': '2',
         'version': '2', 'source': '化验', 'trip': 'T001', '_line': 3},
        {'id': 'L002', 'moisture': '19', 'impurity': '2',
         'version': '2', 'source': '化验', 'trip': 'T001', '_line': 4},
    ]
    facts, bad = merge(rows)
    assert facts == []
    assert {q['code'] for q in bad} == {'CONFLICT'}


def test_lab_row_cannot_carry_weight():
    rows = [{'id': 'L001', 'gross': '1200', 'moisture': '18', 'impurity': '2',
             'version': '2', 'source': '化验', 'trip': 'T001', '_line': 2}]
    facts, bad = merge(rows)
    assert facts == []
    assert bad[0]['code'] == 'SOURCE'


def test_missing_half_is_pending_not_fact():
    rows = [{'id': 'S001', 'bin': '甲仓', 'gross': '1200', 'tare': '200', 'unit': 'kg',
             'version': '1', 'source': '地磅', 'trip': 'T001', '_line': 2}]
    facts, bad, pending, _ = resolve(rows)
    assert facts == []
    assert pending[0]['trip'] == 'T001'
    assert set(pending[0]['missing']) == {'moisture', 'impurity'}
    assert bad[0]['code'] == 'MISSING'


def test_split_files_resume_and_single_entry(isolate):
    first = run(SCALE)
    assert first['records'] == []
    assert first['pending'][0]['trip'] == 'T001'

    second = run(LAB)
    fact = second['records'][0]
    assert fact['id'] == 'S001'
    assert fact['trip'] == 'T001'
    assert (fact['net_kg'], fact['moisture']) == ('1000', '18')
    assert second['balances'][0]['count'] == 1
    assert second['resumed_trips'] == ['T001']

    third = run(SCALE)
    conn = store.connection()
    entries = conn.execute("SELECT trip,bin,net_kg FROM bin_entries").fetchall()
    assert [dict(r) for r in entries] == [
        {'trip': 'T001', 'bin': '甲仓', 'net_kg': '1000'}]
    assert third['balances'][0]['count'] == 1


def test_scale_retry_keeps_original_ticket(isolate):
    first = run(SCALE2)
    assert first['records'] == []
    second = run(SCALE2)
    assert second['records'] == []
    # 两张地磅回执各缺一半，都应保持待续跑且不产生事实
    assert {item['id'] for item in second['pending']} == {'S001', 'S002'}

    run(LAB)
    conn = store.connection()
    status = {row['trip']: row['status'] for row in
              conn.execute("SELECT trip,status FROM trip_status").fetchall()}
    assert status['T001'] == '已入仓'
    assert status['T002'] == '待续跑'
    assert conn.execute("SELECT COUNT(*) FROM bin_entries").fetchone()[0] == 1


def test_switch_failure_never_exposes_half_fact(isolate, monkeypatch):
    run(SCALE)
    original = ledger.switch

    def boom(db, data):
        raise RuntimeError('switch failed')

    monkeypatch.setattr(ledger, 'switch', boom)
    with pytest.raises(RuntimeError):
        run(LAB)
    conn = store.connection()
    assert conn.execute("SELECT COUNT(*) FROM trip_facts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM bin_entries").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM trip_status WHERE trip='T001'").fetchone()[0] == '待续跑'
    assert conn.execute("SELECT status FROM run_log ORDER BY rowid DESC LIMIT 1").fetchone()[0] == '切换失败'

    monkeypatch.setattr(ledger, 'switch', original)
    recovered = run("""id,bin,gross,tare,unit,version,source,trip
S003,乙仓,1300,300,kg,1,地磅,T003
""")
    assert [(f['id'], f['trip'], f['moisture']) for f in recovered['records']] == [('S001', 'T001', '18')]
    assert recovered['resumed_trips'] == ['T001']
    assert conn.execute("SELECT COUNT(*) FROM bin_entries WHERE trip='T001'").fetchone()[0] == 1


def test_field_evidence_export(client):
    r = client.post('/api/runs', json={'text': SCALE}).json
    r = client.post('/api/runs', json={'text': LAB}).json
    response = client.get('/api/evidence-fields.csv?run=' + r['manifest']['run_id'])
    body = response.text
    assert response.status_code == 200
    assert '来源回执' in body and '公式' in body
    lines = body.splitlines()
    assert any(',moisture,18,化验,' in line for line in lines)
    assert any(',gross,1200,地磅,' in line for line in lines)


def test_receipt_page_shows_merged_weight_and_dry_matter(client):
    client.post('/api/runs', json={'text': SCALE})
    client.post('/api/runs', json={'text': LAB})
    page = client.get('/m/3')
    assert page.status_code == 200
    text = page.data.decode()
    assert '收货事实' in text and '字段来源' in text and '合并重量 kg' in text


def test_standard_moisture_and_cap_unchanged(isolate):
    run(SCALE)
    result = run(LAB)
    assert result['rules']['standard_moisture'] == '14'
    assert result['rules']['max_moisture'] == '28'
    assert D(result['records'][0]['standard_kg']) == D(result['records'][0]['dry_kg']) / D('0.86')


def test_lab_over_cap_rejects_trip(isolate):
    run(SCALE)
    result = run("""id,moisture,impurity,version,source,trip
L009,29,2,2,化验,T001
""")
    assert result['records'] == []
    assert any(q['code'] == 'LIMIT' and q['line'] == 2 for q in result['quarantine'])
    conn = store.connection()
    assert conn.execute("SELECT COUNT(*) FROM bin_entries").fetchone()[0] == 0


def test_scale_retry_with_new_receipt_keeps_trip_identity(isolate):
    first = run("""id,bin,gross,tare,unit,version,source,trip
W0001,甲仓,1200,200,kg,1,地磅,T001
""")
    run(LAB)
    retry = run("""id,bin,gross,tare,unit,version,source,trip
W9999,甲仓,1200,200,kg,1,地磅,T001
""")
    facts = retry['records']
    assert len(facts) == 1
    assert facts[0]['trip'] == 'T001'
    assert facts[0]['id'] == 'W0001'          # 原磅单身份不变
    assert facts[0]['moisture'] == '18'        # 复验结果不被重试冲掉
    assert any(q['code'] == 'DUPLICATE' for q in retry['quarantine'])
    conn = store.connection()
    assert conn.execute("SELECT COUNT(*) FROM bin_entries").fetchone()[0] == 1


def test_crash_recovery_resumes_never_completed_trip(isolate):
    run("""id,bin,gross,tare,unit,version,source,trip
S001,甲仓,1200,200,kg,1,地磅,T001
""")
    # 模拟进程在事实切换前中断：回执已落盘，运行仍标记为进行中。
    from app.normalize import serial
    from app.source_contract import receipt_key
    from app.modules.m1_intake.rules import receive
    from app.modules.m2_contract.rules import coerce
    staged = []
    for row in receive(LAB.encode())['raw']:
        parsed = coerce({**row})
        parsed['_key'] = receipt_key(parsed)
        staged.append(serial(parsed))
    conn = store.connection()
    with conn:
        ledger.begin(conn, 'crashedrun')
        ledger.stage(conn, {'run_id': 'crashedrun', 'receipts': staged})
    assert conn.execute("SELECT status FROM run_log WHERE run_id='crashedrun'").fetchone()[0] == '进行中'

    recovered = run("""id,bin,gross,tare,unit,version,source,trip
S009,丙仓,1100,100,kg,1,地磅,T009
""")
    assert recovered['recovered_trips'] == 1
    assert [(f['trip'], f['id'], f['moisture']) for f in recovered['records']] == [('T001', 'S001', '18')]
    assert conn.execute("SELECT status FROM run_log WHERE run_id='crashedrun'").fetchone()[0] == '已恢复'


def test_retest_over_cap_retracts_existing_entry(isolate):
    run("""id,bin,gross,tare,unit,version,source,trip
S001,甲仓,1200,200,kg,1,地磅,T001
""")
    ok = run(LAB)
    assert ok['balances'][0]['count'] == 1
    bad = run("""id,moisture,impurity,version,source,trip
L002,29,2,3,化验,T001
""")
    assert bad['records'] == []
    assert bad['retracted_trips'] == ['T001']
    assert bad['balances'] == []
    conn = store.connection()
    assert conn.execute("SELECT COUNT(*) FROM bin_entries").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM trip_status WHERE trip='T001'").fetchone()[0] == '退收'


def test_conflicting_retest_keeps_existing_fact(isolate):
    run("""id,bin,gross,tare,unit,version,source,trip
S001,甲仓,1200,200,kg,1,地磅,T001
""")
    run(LAB)
    bad = run("""id,moisture,impurity,version,source,trip
L002,19,2,2,化验,T001
""")
    # 同版水杂矛盾：整组隔离，但不撤回已核实的入仓事实。
    assert any(q['code'] == 'CONFLICT' for q in bad['quarantine'])
    conn = store.connection()
    assert conn.execute("SELECT COUNT(*) FROM bin_entries WHERE trip='T001'").fetchone()[0] == 1


def test_conflicting_retest_then_corrected_resubmission(isolate):
    run("""id,bin,gross,tare,unit,version,source,trip
S1,甲仓,1200,200,kg,1,地磅,T1
""")
    run("""id,moisture,impurity,version,source,trip
L1,18,2,2,化验,T1
""")
    clash = run("""id,moisture,impurity,version,source,trip
L2,19,2,2,化验,T1
""")
    assert any(q['code'] == 'CONFLICT' for q in clash['quarantine'])
    # 同版矛盾不撤回已核实事实，但坏回执被停用。
    conn = store.connection()
    assert conn.execute("SELECT COUNT(*) FROM bin_entries WHERE trip='T1'").fetchone()[0] == 1
    assert clash['inactive_receipts']

    fixed = run("""id,moisture,impurity,version,source,trip
L3,17,1,3,化验,T1
""")
    assert [(f['trip'], f['moisture'], f['impurity']) for f in fixed['records']] == [('T1', '17', '1')]
    assert fixed['balances'][0]['count'] == 1


def test_trip_fallback_vehicle_and_time(isolate):
    scale = """id,bin,gross,tare,unit,version,source,vehicle,weigh_time
W1,甲,1200,200,kg,1,地磅,苏A1,2026-11-18T08:00:00+08:00
W2,甲,1210,200,kg,1,地磅,苏A1,2026-11-18T09:00:00+08:00
"""
    lab = """id,moisture,impurity,version,source,vehicle,weigh_time
L1,18,2,2,化验,苏A1,2026-11-18T08:00:00+08:00
L2,19,2,2,化验,苏A1,2026-11-18T09:00:00+08:00
"""
    first = run(scale)
    assert len(first['pending']) == 2
    second = run(lab)
    facts = {f['trip']: f for f in second['records']}
    assert facts['苏A1@2026-11-18T08:00:00+08:00']['moisture'] == '18'
    assert facts['苏A1@2026-11-18T09:00:00+08:00']['moisture'] == '19'
    assert second['balances'][0]['count'] == 2
