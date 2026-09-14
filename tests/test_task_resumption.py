"""Current handoff evidence survives packing; task order stays explicit."""

from __future__ import annotations

from grag.core.types import EdgeRecord, NodeRecord, Subgraph
from grag.retrieval.packing import pack_context_response


def tasks():
    return [NodeRecord(id=f'Task:{i}', label='Task', properties={
        'title': 'Resume this task',
        'body': 'Old pending implementation notes. ' * 12,
        'summary': 'Current: implemented; awaiting verification.',
        'status': 'in_progress', 'priority': i, 'release_status': 'unreleased',
        'acceptance': 'Revoked access must stop within 15 seconds.',
        'next_step': 'Run the revocation integration test.', '_source': 'session:current',
    }) for i in range(3)]


def test_small_resume_context_keeps_current_fields_across_tasks():
    nodes = tasks()
    result = pack_context_response(Subgraph(nodes=nodes), 700, [nodes[0].id])
    assert result.included_node_ids == [n.id for n in nodes]
    assert result.response_token_estimate <= 700
    assert result.truncated and result.omitted_properties == 3
    for node in result.subgraph.nodes:
        assert 'body' not in node.properties
        assert node.properties['release_status'] == 'unreleased'
        assert node.properties['status'] == 'in_progress'
        assert node.properties['summary'] == 'Current: implemented; awaiting verification.'
        assert node.properties['acceptance'] == 'Revoked access must stop within 15 seconds.'
        assert node.properties['next_step'] == 'Run the revocation integration test.'
    # Packing never edits the stored/original narrative or silently substitutes it.
    assert all(n.properties['body'].startswith('Old pending') for n in nodes)


def test_context_order_is_selected_order_not_numeric_priority():
    nodes = tasks()
    order = [nodes[2].id, nodes[0].id, nodes[1].id]
    result = pack_context_response(Subgraph(nodes=nodes), 700, order)
    assert result.included_node_ids == order


def test_full_context_keeps_all_properties_even_conflicting_old_prose():
    nodes = tasks()
    result = pack_context_response(Subgraph(nodes=nodes), 4000, [n.id for n in nodes])
    assert not result.truncated
    assert result.subgraph.nodes == nodes
    assert 'Old pending' in result.context


def test_task_question_and_decision_are_connected_with_current_qualifiers():
    task = tasks()[0]
    question = NodeRecord(id='Question:environment', label='Question', properties={
        'status': 'open', 'question': 'Which environment can run the acceptance check?',
        '_source': 'session:question',
    })
    decision = NodeRecord(id='Decision:ttl', label='Decision', properties={
        'summary': 'Revocation delay must stay within 15 seconds.', '_source': 'policy.md',
        'body': 'Older planning notes. ' * 20,
    })
    edges = [
        EdgeRecord(id='guides', type='GUIDED_BY', source=task.id, target=decision.id),
        EdgeRecord(id='asks', type='RAISES', source=task.id, target=question.id),
    ]
    result = pack_context_response(Subgraph(nodes=[task, question, decision], edges=edges), 850, [task.id])
    assert set(result.included_node_ids) == {task.id, question.id, decision.id}
    assert {edge.id for edge in result.subgraph.edges} == {'guides', 'asks'}
    assert result.subgraph.node_map()[question.id].properties['status'] == 'open'
    assert 'Which environment' in result.context and '15 seconds' in result.context
    assert result.response_token_estimate <= 850
