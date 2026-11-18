"""M3 收货事实与折标：按来源契约合并回执，再按合并后的水杂折标。

地磅补传只带重量、化验复验只带水杂；字段级合并后得到一张可追溯的
收货事实。仓批只按这张事实核算，原回执与逐字段证据都保留在事实上。
"""
from decimal import Decimal as D

from app.errors import DomainError
from app import ledger
from app.source_contract import resolve, attach_dry_matter, receipt_rejection


NUMERIC = ('gross', 'tare', 'net_kg', 'moisture', 'impurity',
           'clean_kg', 'dry_kg', 'standard_kg')


def revive_fact(payload):
    """台账里的事实以 JSON 保存，数值按字符串回来；计算前恢复 Decimal。"""
    fact = dict(payload)
    for key in NUMERIC:
        if key in fact and fact[key] is not None:
            try:
                fact[key] = D(str(fact[key]))
            except Exception:
                pass
    # 台账 payload 用 receipts 保存原回执摘要；恢复成内部字段名供车次归并核对。
    if '_receipts' not in fact:
        fact['_receipts'] = fact.pop('receipts', [])
    fact.setdefault('_line', next((r.get('line', '') for r in fact.get('_receipts', [])
                                   if r.get('contributed')), ''))
    return fact


def _current_trips(rows):
    return {r.get('_trip') or r.get('id', '') for r in rows}


def execute(ctx):
    rules = ctx['rules']
    current = ctx['records']
    trips = _current_trips(current)
    prior, identities, committed = [], {}, {}
    conn = ctx.get('_conn')
    if conn is not None:
        # 续跑两类旧回执：未完成车次（补传后续跑）和本次输入会重算的车次
        # （复验到达后按更高版本字段重新合成）。已完成且未出现在本次输入的
        # 车次不会带入，避免同车次重复入仓。
        wanted = trips | set(ledger.load_pending_trips(conn))
        prior = ledger.load_receipts(wanted, conn)
        identities = ledger.load_identities(trips, conn)
        committed = ledger.load_facts(trips, conn)

    # 本次输入优先：完全相同的台账回执在后面静默复用，隔离只针对新输入。
    facts, bad, pending, parsed = resolve(current + prior)
    ctx['quarantine'].extend(q for q in bad if q.get('origin') == 'current')
    # 不可信的回执在台账里停用，修正后的补传不会被旧坏回执拖住。
    ctx['_inactive'] = sorted({q['receipt'] for q in bad
                               if q.get('receipt') and q.get('inactive')})
    ctx['_parsed_receipts'] = parsed
    ctx['_pending'] = pending

    outcomes = {}
    for item in pending:
        outcomes[item['trip']] = {'status': '待续跑', 'missing': item['missing']}
    for q in bad:
        if q.get('origin') != 'current':
            continue
        if q.get('code') in ('CONFLICT', 'MISSING'):
            trip = q.get('trip') or ''
            if trip:
                outcomes.setdefault(trip, {'status': q['code'], 'missing': []})

    kept, accepted_lines, retracted = [], 0, set()
    for fact in facts:
        try:
            fact = attach_dry_matter(fact, rules)
        except DomainError as exc:
            ctx['quarantine'].extend(
                receipt_rejection(r, exc.code, exc.message)
                for r in fact.get('_receipts', [])
                if r.get('origin') == 'current' and r.get('contributed')
            )
            outcomes[fact['trip']] = {'status': exc.code, 'missing': []}
            if exc.code == 'LIMIT':
                # 有效复验证明水分超收购上限：该车次退收，仓批不再计入。
                retracted.add(fact['trip'])
            continue
        fact['state'] = '已入仓'
        kept.append(fact)
        outcomes[fact['trip']] = {'status': '已入仓', 'missing': []}
        accepted_lines += sum(1 for r in fact.get('_receipts', [])
                              if r.get('origin') == 'current' and r.get('contributed'))

    # 已核实入仓的车次遇到后来的坏回执（同版矛盾、来源不符）时，沿用原事实，
    # 不把仓批里已核实的数据悄悄拿掉；退收只能由有效的水分超限触发。
    covered = {f['trip'] for f in kept}
    carried = []
    for trip, fact in sorted(committed.items()):
        if trip in covered:
            continue
        if outcomes.get(trip, {}).get('status') == 'LIMIT':
            continue
        fact = revive_fact(fact)
        fact['state'] = '沿用已入仓事实'
        fact['_carried'] = True
        carried.append(fact)
        outcomes[trip] = {'status': '已入仓', 'missing': []}
        covered.add(trip)
    kept.extend(carried)

    # 已知的原磅单身份优先：地磅重试不改变磅单号，也不生成第二车次。
    for fact in kept:
        if fact['trip'] in identities:
            fact['id'] = identities[fact['trip']]
            fact['ticket_id'] = fact['id']
    ctx['records'] = kept
    ctx['accepted_lines'] = accepted_lines
    ctx['_carried_trips'] = {f['trip'] for f in kept if f.get('_carried')}
    ctx['_outcomes'] = outcomes
    ctx['_retracted'] = sorted(retracted)
    ctx['_resumed_trips'] = sorted({r.get('trip') or r.get('id', '') for r in prior
                                    if r.get('_origin') == 'prior'} & {f['trip'] for f in kept})
    ctx['stages'].append({
        'title': '收货核对与折标',
        'rows': kept,
        'metrics': {
            '车次事实': len(kept),
            '折标 kg': str(sum((r['standard_kg'] for r in kept), D(0))),
            '干物质 kg': str(sum((r['dry_kg'] for r in kept), D(0))),
            '合并重量 kg': str(sum((r['net_kg'] for r in kept), D(0))),
            '待续跑': len(pending),
            '拒收': sum(q['code'] == 'LIMIT' for q in ctx['quarantine']),
        },
    })
    return ctx
