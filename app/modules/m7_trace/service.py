from .rules import audit


def execute(ctx):
    # 沿用已入仓事实的车次不是本轮输入回执，不参与本轮行数平衡。
    current = [r for r in ctx['records'] if not r.get('_carried')]
    ctx['audit'] = audit(current, ctx['quarantine'], ctx['input_count'],
                          ctx.get('accepted_lines'))
    ctx['audit']['count'] = len(ctx['records'])
    ctx['audit']['carried'] = len(ctx['records']) - len(current)
    rows = [{'code': code, 'count': count} for code, count in sorted(ctx['audit']['causes'].items())]
    ctx['stages'].append({'title': '批次追溯', 'rows': rows, 'metrics': {
        '输入': ctx['input_count'], '采信车次': len(ctx['records']),
        '采信回执': ctx['audit']['accepted_lines'], '隔离': len(ctx['quarantine']),
    }})
    return ctx
