"""Fresh source-backed MCP sessions resume scoped work using the existing tools."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from contextlib import asynccontextmanager, closing
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from grag.config import GragConfig
from grag.core.types import DefineSchemaRequest, UpsertNodesRequest
from grag.project_files import apply_ops
from grag.project_identity import identity_ops, new_identity
from grag.service import GragService


def map_checkout(root: Path, db: Path):
    root.mkdir(parents=True)
    (root / 'nested').mkdir()
    identity = new_identity(root.resolve(), db)
    with patch.object(Path, 'home', return_value=root / 'isolated-home'):
        apply_ops(identity_ops(root, identity))
    return identity


def seed(root: Path, db: Path, marker: str):
    identity = map_checkout(root, db)
    source = root / 'handoff.md'
    source.write_text(f'{marker}: current acceptance and release evidence.\n', encoding='utf-8')
    with closing(GragService(GragConfig(db_path=db, buffer_pool_size=128*1024**2))) as service:
        service.define_schema(DefineSchemaRequest.model_validate({'node_tables': [
            {'name': 'Task', 'properties': [
                {'name': key, 'type': 'INT64' if key in {'priority', 'mission_order'} else 'STRING'}
                for key in ('checkout_id', 'title', 'summary', 'body', 'status', 'priority',
                            'mission_order', 'release_status', 'released_in', 'acceptance', 'next_step')
            ]},
            {'name': 'Decision', 'properties': [{'name': 'summary'}, {'name': 'body'}]},
            {'name': 'Question', 'properties': [{'name': name} for name in ('checkout_id', 'question', 'status')]},
        ], 'rel_tables': [
            {'name': 'GUIDED_BY', 'from_label': 'Task', 'to_label': 'Decision'},
            {'name': 'RAISES', 'from_label': 'Task', 'to_label': 'Question'},
        ]}))
        nodes = []
        for key, status, priority, order, scope in [
            ('resume', 'open', 2, 99, identity.checkout_id),
            ('later', 'open', 8, 1, identity.checkout_id),
            ('finished', 'done', 0, 2, identity.checkout_id),
            ('blocked', 'blocked', 1, 3, identity.checkout_id),
            ('foreign', 'open', 0, 4, 'other-checkout'),
            ('unknown-scope', 'open', 0, 5, None),
            ('superseded', 'open', 0, 6, identity.checkout_id),
            ('expired', 'open', 1, 7, identity.checkout_id),
        ]:
            node = {'label': 'Task', 'key': key, 'source': str(source), 'properties': {
                'checkout_id': scope, 'title': f'{marker}: {key}',
                'summary': f'{marker}: verify revocation' if key == 'resume' else f'{marker}: {key}',
                'body': 'Historical pending and unreleased planning notes. ' * 30,
                'status': status, 'priority': priority, 'mission_order': order,
                'release_status': 'released' if key == 'finished' else 'unreleased',
                'released_in': '0.9.0' if key == 'finished' else None,
                'acceptance': 'Revoked access stops within 15 seconds.',
                'next_step': 'Run the revocation acceptance check.',
            }}
            if key == 'superseded':
                node['evidence'] = {'state': 'superseded', 'superseded_by': 'Task:resume'}
            if key == 'expired':
                node['evidence'] = {'expires_at': '2000-01-01T00:00:00+00:00'}
            nodes.append(node)
        nodes.extend([
            {'label': 'Decision', 'key': 'ttl', 'source': str(source), 'properties': {
                'summary': 'Revocation must take effect within 15 seconds.',
                'body': 'Old rationale details. ' * 60,
            }},
            *({'label': 'Question', 'key': key, 'source': str(source),
                **({'evidence': {'expires_at': '2000-01-01T00:00:00+00:00'}} if key == 'expired-question' else {}),
                'properties': {
                'checkout_id': identity.checkout_id, 'question': text, 'status': status,
            }} for key, text, status in [
                ('open', 'Does staging meet the revocation acceptance check?', 'open'),
                ('answered', 'Which cache store is used?', 'answered'),
                ('expired-question', 'Does the retired environment pass?', 'open'),
            ]),
        ])
        result = service.upsert_nodes(UpsertNodesRequest.model_validate({'nodes': nodes, 'edges': [
            {'type': 'GUIDED_BY', 'from_label': 'Task', 'from_key': 'resume', 'to_label': 'Decision', 'to_key': 'ttl'},
            *({'type': 'RAISES', 'from_label': 'Task', 'from_key': 'resume', 'to_label': 'Question', 'to_key': key} for key in ('open', 'answered', 'expired-question')),
        ]}))
        assert not result.warnings
    return identity


async def call(session, name, args):
    response = await session.call_tool(name, args)
    text = '\n'.join(c.text for c in response.content if c.type == 'text')
    assert not response.is_error, text
    return text, json.loads(text.rsplit('\n---\n', 1)[-1])


@asynccontextmanager
async def connect(root):
    launcher = os.environ.get('GRAG_TEST_COMMAND')
    env = {**get_default_environment(), 'PATH': os.environ.get('PATH', ''),
           'GRAG_EMBED_PROVIDER': '', 'GRAG_AUTO_REFRESH_CODE': '0', 'GRAG_BUFFER_POOL_MB': '128'}
    if os.environ.get('LBUG_PYTHON_BACKEND') == 'capi':
        env['LBUG_PYTHON_BACKEND'] = 'capi'
    if not launcher:
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    params = StdioServerParameters(command=launcher or sys.executable,
                                  args=['mcp'] if launcher else ['-m', 'grag.cli', 'mcp'],
                                  cwd=str(root / 'nested'), env=env)
    with (root / 'mcp.stderr').open('a') as error:
        async with (
            stdio_client(params, errlog=error) as (read, write),
            ClientSession(read, write, read_timeout_seconds=60) as session,
        ):
            await session.initialize()
            yield session


async def resume(session, checkout):
    schema, _ = await call(session, 'describe_schema', {})
    # MCP exposes a readable schema, not a JSON SchemaDocument in its footer.
    # This scripted fixture recognizes its declared primitive properties only;
    # it is not a general schema adapter or an autonomous task-selection API.
    table = next((line for line in schema.splitlines() if line.startswith('Task(')), None)
    if table is None:
        return {'state': 'no_task_schema'}
    properties = set(re.findall(r'(?:\(|, )([a-z_]+):', table))
    if not {'checkout_id', 'title', 'summary', 'status', 'priority'}.issubset(properties):
        return {'state': 'adapt_to_existing_schema'}
    # This fixture declares lower numbers as higher priority. mission_order
    # deliberately disagrees and must never be substituted for that convention.
    _, shortlist = await call(session, 'cypher_query', {'cypher': (
        f"MATCH (t:Task) WHERE t.checkout_id='{checkout}' AND t.status IN ['open','in_progress'] "
        'RETURN t.id,t.title,t.status,t.priority,t.summary '
        'ORDER BY CASE WHEN t.priority IS NULL THEN 1 ELSE 0 END,t.priority,t.id LIMIT 8'
    )})
    if not shortlist['rows']:
        return {'state': 'no_matching_tasks', 'truncated': shortlist['truncated']}
    _, eligible = await call(session, 'get_context', {
        'node_ids': [f'Task:{row[0]}' for row in shortlist['rows']],
        'hops': 0, 'token_budget': 1600,
    })
    rows = [row for row in shortlist['rows'] if f'Task:{row[0]}' in eligible['included_node_ids']]
    if eligible['omitted_nodes']:
        return {'state': 'incomplete_shortlist_context'}
    if not rows:
        return {'state': 'no_current_candidate_in_shortlist', 'eligibility': eligible}
    if any(row[3] is None for row in rows) or (len(rows) > 1 and rows[0][3] == rows[1][3]):
        return {'state': 'priority_uncertain', 'candidates': rows}
    key = rows[0][0]
    context, metadata = await call(session, 'get_context', {
        'node_ids': [f'Task:{key}'], 'hops': 1, 'token_budget': 1600,
    })
    _, questions = await call(session, 'cypher_query', {'cypher': (
        f"MATCH (:Task {{id:'{key}'}})-[:RAISES]->(q:Question) "
        f"WHERE q.checkout_id='{checkout}' AND q.status='open' RETURN q.id,q.question ORDER BY q.id LIMIT 8"
    )})
    current_questions = []
    if questions['rows']:
        _, question_evidence = await call(session, 'get_context', {
            'node_ids': [f'Question:{row[0]}' for row in questions['rows']],
            'hops': 0, 'token_budget': 1200,
        })
        if question_evidence['omitted_nodes']:
            return {'state': 'incomplete_question_context'}
        current_questions = [row for row in questions['rows']
                             if f'Question:{row[0]}' in question_evidence['included_node_ids']]
    return {'state': 'selected', 'task': rows[0], 'context': context, 'metadata': metadata,
            'questions': current_questions, 'excluded': eligible.get('excluded_evidence', 0)}


def test_fresh_mcp_sessions_resume_scoped_current_work_and_preserve_history(tmp_path):
    asyncio.run(workflow(tmp_path))


async def workflow(tmp_path):
    root_a, root_b = tmp_path / 'one/repo', tmp_path / 'two/repo'
    a = seed(root_a, tmp_path / 'a.lbdb', 'A')
    b = seed(root_b, tmp_path / 'b.lbdb', 'B')
    async with connect(root_a) as session:
        selected = await resume(session, a.checkout_id)
        assert selected['state'] == 'selected' and selected['task'][0] == 'resume'
        assert selected['excluded'] == 2  # superseded/expired still say status=open
        assert selected['task'][1].startswith('A:')
        assert selected['questions'] == [['open', 'Does staging meet the revocation acceptance check?']]
        assert selected['metadata']['response_token_estimate'] <= 1600
        assert 'Decision:ttl' in selected['metadata']['included_node_ids']
        assert 'Revoked access stops within 15 seconds.' in selected['context']
        _, release = await call(session, 'cypher_query', {'cypher': "MATCH (t:Task {id:'finished'}) RETURN t.status,t.release_status,t.released_in"})
        assert release['rows'] == [['done', 'released', '0.9.0']]
        _, current = await call(session, 'cypher_query', {'cypher': "MATCH (t:Task {id:'resume'}) RETURN t"})
        update = {'operation_id': 'm21-handoff', 'nodes': [{
            'label': 'Task', 'key': 'resume', 'expected_revision': current['rows'][0][0]['_revision'],
            'source': str(root_a / 'handoff.md'), 'properties': {
                'body': 'Current implementation is ready; review the acceptance result.',
                'summary': 'A: acceptance verified; review next.', 'status': 'in_progress',
                'next_step': 'Review the acceptance result.',
            }, 'evidence': {'actor': 'M21 regression', 'reason': 'Replace accumulated planning with a current handoff.'},
        }]}
        await call(session, 'upsert_nodes', update)
    async with connect(root_b) as session:
        other = await resume(session, b.checkout_id)
        assert other['task'][1].startswith('B:') and other['task'][4] == 'B: verify revocation'
    async with connect(root_a) as session:
        selected = await resume(session, a.checkout_id)
        assert selected['task'][4] == 'A: acceptance verified; review next.'
        assert 'Historical pending' not in selected['context']
        _, replay = await call(session, 'upsert_nodes', update)
        assert replay['replayed']
        _, current = await call(session, 'cypher_query', {'cypher': "MATCH (t:Task {id:'resume'}) RETURN t"})
        await call(session, 'upsert_nodes', {'nodes': [{
            'label': 'Task', 'key': 'resume', 'expected_revision': current['rows'][0][0]['_revision'],
            'source': str(root_a / 'handoff.md'), 'properties': {
                'status': 'done', 'summary': 'Implemented and verified locally.',
                'body': 'Acceptance passed. Release remains a separate step.', 'next_step': None,
            }, 'evidence': {'actor': 'M21 regression', 'reason': 'Acceptance completed; still unreleased.'},
        }]})
    async with connect(root_a) as session:
        selected = await resume(session, a.checkout_id)
        assert selected['task'][0] == 'later'  # completed but unreleased is not open work
        _, history = await call(session, 'get_context', {'node_ids': ['Task:resume'], 'history': True, 'token_budget': 1600})
        assert [e['sequence'] for e in history['history']['entries']] == [2, 1, 0]
        old, _ = await call(session, 'get_context', {'node_ids': ['Task:resume'], 'revision': 0, 'text_property': 'body', 'token_budget': 1600})
        assert 'Historical pending' in old
        _, done = await call(session, 'cypher_query', {'cypher': "MATCH (t:Task {id:'resume'}) RETURN t.status,t.release_status,t.released_in"})
        assert done['rows'] == [['done', 'unreleased', None]]
        unknown = await resume(session, 'not-the-selected-checkout')
        assert unknown['state'] == 'no_matching_tasks'
        # Missing/tied priority is surfaced, never resolved by task ID/order.
        await call(session, 'upsert_nodes', {'nodes': [{'label': 'Task', 'key': 'later', 'properties': {'priority': None}}]})
        assert (await resume(session, a.checkout_id))['state'] == 'priority_uncertain'
        await call(session, 'upsert_nodes', {'nodes': [
            {'label': 'Task', 'key': 'later', 'properties': {'priority': 8}},
            {'label': 'Task', 'key': 'blocked', 'properties': {'priority': 8, 'status': 'open'}},
        ]})
        assert (await resume(session, a.checkout_id))['state'] == 'priority_uncertain'


@pytest.mark.parametrize('has_task', [False, True], ids=['no-task-label', 'unfamiliar-task-schema'])
def test_resume_does_not_invent_or_migrate_task_schema(tmp_path, has_task):
    root, db = tmp_path / 'repo', tmp_path / 'memory.lbdb'
    identity = map_checkout(root, db)
    with closing(GragService(GragConfig(db_path=db, buffer_pool_size=128*1024**2))) as service:
        if has_task:
            service.define_schema(DefineSchemaRequest.model_validate({
                'node_tables': [{'name': 'Task', 'properties': [{'name': 'body'}]}],
            }))

    async def check():
        async with connect(root) as session:
            before, _ = await call(session, 'describe_schema', {})
            result = await resume(session, identity.checkout_id)
            assert result['state'] == ('adapt_to_existing_schema' if has_task else 'no_task_schema')
            after, _ = await call(session, 'describe_schema', {})
            assert before == after

    asyncio.run(check())
