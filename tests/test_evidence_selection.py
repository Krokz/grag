"""M27: exact partial evidence, citations and useful graph connections."""
from __future__ import annotations

import hashlib
import json

import pytest

from grag.core.serialize import estimate_tokens
from grag.core.types import EdgeRecord, NodeRecord, ScoredNode, Subgraph
from grag.retrieval.packing import (
    mcp_retrieval_text,
    pack_context_response,
    pack_search_response,
)
from workflow_eval import score_evidence


def search(graph, query, budget=1500, ids=None):
    ids = ids or [graph.nodes[0].id]
    nodes = graph.node_map()
    seeds = [ScoredNode(node=nodes[nid], score=1.0/(i+1), match="fts") for i,nid in enumerate(ids)]
    return pack_search_response(graph, seeds, budget, query=query)


def long_body():
    return ('Background for rotation. 日本語 אבג 🌍.\n' * 150 +
            'Do not revoke existing tokens during ordinary refresh. '
            'Service accounts must reauthenticate after signing-key rotation. '
            'The exception applies only when a signing key changes. ' +
            'More operational history. ' * 150)


@pytest.mark.parametrize('budget', [256, 512, 1000, 3000])
def test_excerpts_are_exact_and_explicit_and_transport_bounded(budget):
    body = long_body()
    node = NodeRecord(id='Note:rotation', label='Note', properties={'body':body, '_source':'design.md', 'status':'draft'})
    graph = Subgraph(nodes=[node])
    original = graph.model_dump_json()
    result = search(graph, 'signing-key rotation for service accounts', budget)
    assert result.response_token_estimate <= budget
    assert estimate_tokens(result.model_dump_json()) <= budget
    assert estimate_tokens(mcp_retrieval_text(result)) <= budget
    assert result.truncated
    if budget >= 1000:
        assert len(result.text_excerpts)==1
        assert result.subgraph.nodes[0].properties['status']=='draft'
        assert 'body' not in result.subgraph.nodes[0].properties
        assert result.omitted_properties==1
        excerpt = result.text_excerpts[0]
        assert excerpt.text == body[excerpt.offset:excerpt.end]
        assert excerpt.sha256 == hashlib.sha256(body.encode('utf-8')).hexdigest()
        assert excerpt.total_chars == len(body)
        assert 'must reauthenticate' in excerpt.text
        # Preserve surrounding qualification instead of returning only a hit.
        assert 'Do not revoke' in excerpt.text and 'only when' in excerpt.text
        wire = mcp_retrieval_text(result)
        assert '[excerpt body chars ' in wire
        footer = json.loads(wire.rsplit('\n---\n',1)[-1])
        assert footer['text_excerpts']==[excerpt.model_dump(exclude={'text'})]
    assert graph.model_dump_json()==original


def test_full_values_win_and_unmatched_or_unqueried_text_is_not_excerpted():
    body = long_body()
    node = NodeRecord(id='Note:rotation', label='Note', properties={'body':body, '_source':'design.md'})
    graph = Subgraph(nodes=[node])
    full = search(graph,'signing-key rotation',budget=30_000)
    assert full.subgraph.nodes[0].properties['body']==body and not full.text_excerpts
    assert not full.truncated
    for response in [search(graph,'banana horticulture'), pack_context_response(graph,1500,[node.id])]:
        assert not response.text_excerpts and 'body' not in response.subgraph.nodes[0].properties
        assert response.truncated


def test_expansion_prioritizes_relevant_neighbor_without_changing_seeds_or_direction():
    root = NodeRecord(id='Component:auth', label='Component', properties={'summary':'Authorize sessions','_source':'auth.py'})
    distractors = [NodeRecord(id=f'Note:background-{i}',label='Note',properties={'body':'Other work','_source':'other.md'}) for i in range(40)]
    decisive = NodeRecord(id='Decision:last',label='Decision',properties={'body':'Signing-key rotation immediately invalidates service accounts.','_source':'decision.md'})
    edges = [EdgeRecord(id=f'R:{n.id}',type='EXPLAINS',source=n.id,target=root.id) for n in [*distractors,decisive]]
    first = search(Subgraph(nodes=[root,*distractors,decisive],edges=edges),'signing-key rotation service accounts',budget=1200)
    reversed_input = search(Subgraph(nodes=[root,decisive,*reversed(distractors)],edges=list(reversed(edges))),
                            'signing-key rotation service accounts',budget=1200)
    assert first.model_dump()==reversed_input.model_dump()
    assert [s.node.id for s in first.seeds]==[root.id]
    assert decisive.id in first.included_node_ids
    assert 'immediately invalidates' in first.context
    assert any((e.source,e.type,e.target)==(decisive.id,'EXPLAINS',root.id) for e in first.subgraph.edges)
    assert first.omitted_nodes and first.omitted_edges


def test_bridges_between_seeds_survive_even_without_matching_query_words():
    nodes = [NodeRecord(id=f'Note:{key}', label='Note', properties={'body':body,'_source':'notes.md'})
             for key,body in [('left','origin'),('right','destination'),('bridge','intermediate dependency'),
                              *[(f'noise-{i}','query query query') for i in range(30)]]]
    edges = [EdgeRecord(id='left-bridge',type='USES',source='Note:left',target='Note:bridge'),
             EdgeRecord(id='bridge-right',type='USES',source='Note:bridge',target='Note:right')]
    edges += [EdgeRecord(id=n.id,type='USES',source='Note:left',target=n.id) for n in nodes[3:]]
    result = search(Subgraph(nodes=nodes,edges=edges),'query',budget=1400,ids=['Note:left','Note:right'])
    assert {'left-bridge','bridge-right'} <= {e.id for e in result.subgraph.edges}
    assert result.subgraph.node_map()['Note:bridge'].properties['body']=='intermediate dependency'


def test_other_nodes_and_edge_evidence_precede_bookkeeping():
    nodes = [NodeRecord(id=f'Note:{key}',label='Note',properties={
        'id':key*2000,'_created_at':'irrelevant '*500,'body':f'Evidence {key}', '_source':'notes.md'}) for key in ['a','b']]
    edge = EdgeRecord(id='explanation',type='EXPLAINS',source='Note:a',target='Note:b',properties={'reason':'Only during maintenance.'})
    response = pack_context_response(Subgraph(nodes=nodes,edges=[edge]),700,['Note:a','Note:b'])
    assert all(n.properties.get('body')==f'Evidence {n.id[-1]}' for n in response.subgraph.nodes)
    assert len(response.subgraph.nodes)==2
    assert response.subgraph.edges[0].properties['reason']=='Only during maintenance.'
    assert not any('id' in n.properties or '_created_at' in n.properties for n in response.subgraph.nodes)


def test_code_citations_keep_the_range_before_ids_and_metadata():
    nodes = [NodeRecord(id=f'Function:{key}',label='Function',properties={
        'id':'x'*5000,'name':key,'docstring':f'Function {key} behavior.', 'line_start':4,'line_end':8,
        'language':'python','_source':f'{key}.py','_created_at':'y'*5000}) for key in ['a','b']]
    response = pack_context_response(Subgraph(nodes=nodes),700,[n.id for n in nodes])
    assert len(response.subgraph.nodes)==2
    for node in response.subgraph.nodes:
        assert node.properties['line_start']==4 and node.properties['line_end']==8
        assert node.properties['_source']==f'{node.id[-1]}.py'
        assert node.properties['docstring']


def test_evaluator_refuses_fabricated_excerpt_even_if_it_contains_gold_words(tmp_path):
    body=long_body()
    source=tmp_path/'notes.md'
    source.write_text(body,encoding='utf-8')
    node=NodeRecord(id='Note:rotation',label='Note',properties={'body':body,'_source':str(source)})
    response=search(Subgraph(nodes=[node]),'signing-key rotation service accounts')
    case={'nodes':[node.id],'seeds':[node.id],'edges':[],'text':['must reauthenticate']}
    values={(node.id,'body'):body}
    assert score_evidence(case,response,{node.id:node.id},text_values=values)['complete_evidence']
    response.text_excerpts[0].offset+=1
    scored=score_evidence(case,response,{node.id:node.id},text_values=values)
    assert not scored['complete_evidence'] and scored['invalid_excerpts']


def test_giant_sentence_and_unavailable_citation_do_not_produce_misleading_snippets():
    for body, source in [('service accounts signing-key rotation '*1000, 'notes.md'),
                         (long_body(), 'huge/'*2000)]:
        node = NodeRecord(id='Note:long',label='Note',properties={'body':body,'_source':source})
        response=search(Subgraph(nodes=[node]),'service accounts signing-key rotation',budget=700)
        assert response.truncated and not response.text_excerpts
        assert all('body' not in n.properties for n in response.subgraph.nodes)


def test_rest_mcp_excerpt_metadata_and_existing_pager_agree(tmp_path):
    import asyncio

    from fastapi.testclient import TestClient

    from grag.api.main import create_app
    from grag.config import GragConfig
    from grag.mcp_server.server import create_server

    cfg=GragConfig(db_path=tmp_path/'evidence.lbdb',buffer_pool_size=128*1024*1024,auto_refresh_code=False)
    app=create_app(cfg)
    server=create_server(cfg,registry=app.state.registry)
    body=long_body()
    source=tmp_path/'notes.md'
    source.write_text(body,encoding='utf-8')
    with TestClient(app) as client:
        assert client.post('/api/schema/define',json={'node_tables':[{'name':'Evidence','properties':[{'name':'body'}]}]}).status_code==200
        assert client.post('/api/nodes/upsert',json={'nodes':[{'label':'Evidence','key':'rotation','properties':{'body':body},'source':str(source)}]}).status_code==200
        args={'query':'signing-key rotation service accounts','labels':['Evidence'],'top_k':1,'hops':0,'token_budget':1600}
        rest=client.post('/api/search',json=args)
        assert rest.status_code==200
        payload=rest.json()
        assert payload['truncated'] and len(payload['text_excerpts'])==1
        excerpt=payload['text_excerpts'][0]
        assert excerpt['text']==body[excerpt['offset']:excerpt['end']]
        mcp=asyncio.run(server.call_tool('search_knowledge',args))
        assert not mcp.is_error
        context,footer=mcp.content[0].text.rsplit('\n\n---\n',1)
        assert context==payload['context']
        assert json.loads(footer)['text_excerpts']==[{k:v for k,v in excerpt.items() if k!='text'}]
        assert 'body' not in payload['subgraph']['nodes'][0]['properties']
        page_args={'node_ids':[excerpt['node_id']],'text_property':excerpt['property'],
                   'text_offset':excerpt['offset'],'text_sha256':excerpt['sha256'],'token_budget':1600}
        page=client.post('/api/context',json=page_args)
        assert page.status_code==200
        page_text=page.json()['subgraph']['nodes'][0]['properties']['body']
        assert page_text.startswith(excerpt['text'])
        assert client.post('/api/nodes/upsert',json={'nodes':[{'label':'Evidence','key':'rotation','properties':{'body':'changed'},'source':str(source)}]}).status_code==200
        stale=client.post('/api/context',json=page_args)
        assert stale.status_code==404 and stale.json()['code']=='not_found'
        mcp_stale=asyncio.run(server.call_tool('get_context',page_args))
        assert mcp_stale.is_error and mcp_stale.structured_content==stale.json()
