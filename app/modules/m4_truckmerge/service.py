from .rules import audit_facts


def execute(ctx):
    good, bad = audit_facts(ctx['records'])
    ctx['records'] = good
    ctx['quarantine'].extend(bad)
    ctx['stages'].append({'title': '车次归并', 'rows': good, 'metrics': {
        '保留车次': len(good),
        '冲突': sum(q['code'] == 'CONFLICT' for q in bad),
        '源回执': sum(len(r.get('_receipts') or []) for r in good),
    }})
    return ctx
