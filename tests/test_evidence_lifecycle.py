"""M19: lifecycle selection, durable guarded history and retained documents."""
from __future__ import annotations

import datetime as dt

import pytest

from grag.core.errors import ConflictError, GragError, NotFoundError, SchemaError
from grag.core.mutate import define_schema, upsert_nodes
from grag.core.revisions import content_revision
from grag.core.types import (
    ContextRequest,
    DefineSchemaRequest,
    EvidenceUpdate,
    IngestDocument,
    IngestRequest,
    NodeTableSpec,
    PropertySpec,
    RelTableSpec,
    SearchRequest,
    UpsertEdge,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.retrieval.context import get_context
from grag.retrieval.search import search_knowledge


def setup(engine):
    define_schema(engine, engine.config, DefineSchemaRequest(
        node_tables=[NodeTableSpec(name='Note', properties=[PropertySpec(name=k) for k in ['body','status']])],
        rel_tables=[RelTableSpec(name='LINKS', from_label='Note', to_label='Note')]))


def write(engine, key, body=None, **kwargs):
    return upsert_nodes(engine, engine.config, UpsertNodesRequest(nodes=[UpsertNode(
        label='Note', key=key, properties={'body':body} if body is not None else {}, **kwargs)]))


def node(engine, key):
    return engine.execute('MATCH (n:Note {id:$key}) RETURN n', {'key':key}).rows[0][0]


def context(engine, key, **kwargs):
    return get_context(engine,engine.config,ContextRequest(node_ids=['Note:'+key], **kwargs))


def test_adoption_correction_history_paging_and_replay(engine):
    setup(engine)
    write(engine,'policy','Old cache rule, 30 minutes.',source='old.md')
    original = node(engine,'policy')
    req=UpsertNodesRequest(operation_id='correction-1', nodes=[UpsertNode(label='Note',key='policy',
        properties={'body':'Current cache rule, 15 seconds.'},source='new.md',expected_revision=content_revision(original),
        evidence=EvidenceUpdate(actor='Claude Code',reason='Correct the earlier proposal',review='accepted'))])
    first=upsert_nodes(engine,engine.config,req)
    assert upsert_nodes(engine,engine.config,req).replayed
    assert node(engine,'policy')['_created_at']==original['_created_at']
    history=context(engine,'policy',history=True,token_budget=3000)
    assert history.history and [e.sequence for e in history.history.entries]==[1,0]
    assert history.history.entries[0].actor=='Claude Code'
    assert history.history.entries[1].source=='old.md' and history.history.entries[1].baseline
    assert history.history.entries[1].actor is None
    old=context(engine,'policy',revision=0,token_budget=3000)
    assert '30 minutes' in old.context and 'old.md' in old.context and '_history_revision: 0' in old.context
    write(engine,'policy','Second correction.',source='second.md',expected_revision=first.revisions['Note:policy'])
    assert node(engine,'policy')['_evidence_seq']==2
    assert upsert_nodes(engine,engine.config,req).replayed
    assert node(engine,'policy')['body']=='Second correction.'
    assert len(context(engine,'policy',history=True,token_budget=3000).history.entries)==3
    page=context(engine,'policy',revision=1,text_property='body',token_budget=1000)
    assert '15 seconds' in page.context and page.text_page.next_offset is None
    write(engine,'policy')
    assert node(engine,'policy')['_evidence_seq']==2  # no-op does not invent an edit


def test_lifecycle_filters_seeds_paths_and_preserves_tasks(engine):
    setup(engine)
    upsert_nodes(engine,engine.config,UpsertNodesRequest(nodes=[
        UpsertNode(label='Note',key='current',properties={'body':'cache policy fifteen seconds','status':'current'}),
        UpsertNode(label='Note',key='old',properties={'body':'cache policy thirty minutes','status':'superseded'}),
        UpsertNode(label='Note',key='far',properties={'body':'unrelated far neighbor'}),
        UpsertNode(label='Note',key='done',properties={'body':'cache task completed','status':'done'})], edges=[
        UpsertEdge(type='LINKS',from_label='Note',from_key='current',to_label='Note',to_key='old'),
        UpsertEdge(type='LINKS',from_label='Note',from_key='old',to_label='Note',to_key='far')]))
    result=search_knowledge(engine,engine.config,SearchRequest(query='cache',hops=2,token_budget=5000))
    assert 'Note:old' not in result.included_node_ids and 'thirty minutes' not in result.context
    assert 'Note:done' in result.included_node_ids
    result=context(engine,'current',hops=2,token_budget=5000)
    assert result.included_node_ids==['Note:current'] and not result.subgraph.edges
    assert result.excluded_evidence==1
    old=context(engine,'old',token_budget=5000,evidence='all')
    assert 'superseded' in old.context and 'thirty minutes' in old.context
    assert not context(engine,'old').included_node_ids
    with pytest.raises(NotFoundError):
        context(engine,'old',text_property='body')
    assert 'superseded' in context(engine,'old',text_property='body',evidence='all').context


def test_old_hits_cannot_crowd_out_current_shortlist(engine):
    setup(engine)
    upsert_nodes(engine,engine.config,UpsertNodesRequest(nodes=[
        UpsertNode(label='Note',key=f'old-{i}',properties={'body':'cache policy cache policy','status':'superseded'}) for i in range(50)
    ]+[UpsertNode(label='Note',key='new',properties={'body':'cache policy current fifteen seconds'})]))
    result=search_knowledge(engine,engine.config,SearchRequest(query='cache policy',top_k=1,hops=0,token_budget=3000))
    assert result.included_node_ids==['Note:new']


def test_review_expiry_and_supersession_are_explicit_guarded_edits(engine):
    setup(engine)
    write(engine,'new','new policy')
    write(engine,'old','old policy',evidence=EvidenceUpdate(actor='Cursor'))
    initial=content_revision(node(engine,'old'))
    with pytest.raises(SchemaError):
        write(engine,'old',evidence=EvidenceUpdate(state='retracted'))
    write(engine,'old',expected_revision=initial,evidence=EvidenceUpdate(state='superseded',superseded_by='Note:new'))
    assert not context(engine,'old').included_node_ids
    with pytest.raises(ConflictError):
        write(engine,'old','losing edit',expected_revision=initial)
    write(engine,'old',expected_revision=content_revision(node(engine,'old')),
          evidence=EvidenceUpdate(state='current',superseded_by=None,review='disputed'))
    assert not context(engine,'old').included_node_ids
    write(engine,'old',expected_revision=content_revision(node(engine,'old')),
          evidence=EvidenceUpdate(review='accepted',expires_at=dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=1)))
    assert not context(engine,'old').included_node_ids
    assert '_expires_at' in context(engine,'old',evidence='all',token_budget=3000).context
    write(engine,'old',expected_revision=content_revision(node(engine,'old')),evidence=EvidenceUpdate(expires_at=None))
    assert context(engine,'old').included_node_ids==['Note:old']


def test_failed_batch_rolls_back_history_and_receipt(engine):
    setup(engine)
    write(engine,'old','stable',evidence=EvidenceUpdate())
    before=node(engine,'old')
    req=UpsertNodesRequest(operation_id='bad',nodes=[UpsertNode(label='Note',key='old',properties={'body':'bad'},
        expected_revision=content_revision(before),evidence=EvidenceUpdate(state='superseded',superseded_by='Note:missing'))])
    with pytest.raises(NotFoundError):
        upsert_nodes(engine,engine.config,req)
    assert node(engine,'old')['body']=='stable'
    assert len(context(engine,'old',history=True).history.entries)==1
    assert content_revision(node(engine,'old'))==content_revision(before)


def test_document_retention_is_obsolete_and_reintroduced_text_current(engine):
    from grag.core.mutate import upsert_edges
    from grag.core.types import UpsertEdgesRequest
    from grag.ingest.loaders import ingest_documents
    setup(engine)
    write(engine,'agent','authored annotation')
    define_schema(engine,engine.config,DefineSchemaRequest(node_tables=[NodeTableSpec(name='Chunk',properties=[PropertySpec(name='text'),PropertySpec(name='meta')])],rel_tables=[RelTableSpec(name='CITES',from_label='Note',to_label='Chunk')]))
    ingest_documents(engine,engine.config,IngestRequest(documents=[IngestDocument(text='cache old obsolete rule '*30,source='doc.md')],chunk_size=80,chunk_overlap=0))
    key=engine.execute('MATCH (n:Chunk) RETURN n.id ORDER BY n.id DESC LIMIT 1').rows[0][0]
    upsert_edges(engine,engine.config,UpsertEdgesRequest(edges=[UpsertEdge(type='CITES',from_label='Note',from_key='agent',to_label='Chunk',to_key=key)]))
    result=ingest_documents(engine,engine.config,IngestRequest(documents=[IngestDocument(text='cache fresh rule',source='doc.md')],chunk_size=80,chunk_overlap=0))
    assert result.warnings
    row=engine.execute('MATCH (n:Chunk {id:$key}) RETURN n',{'key':key}).rows[0][0]
    assert row['_document_state']=='obsolete'
    current=search_knowledge(engine,engine.config,SearchRequest(query='cache',labels=['Chunk'],token_budget=4000))
    assert 'obsolete rule' not in current.context
    old=get_context(engine,engine.config,ContextRequest(node_ids=['Chunk:'+key],evidence='all',token_budget=3000))
    assert '_document_state: "obsolete"' in old.context
    ingest_documents(engine,engine.config,IngestRequest(documents=[IngestDocument(text='cache old obsolete rule '*30,source='doc.md')],chunk_size=80,chunk_overlap=0))
    assert engine.execute('MATCH (n:Chunk {id:$key}) RETURN n._document_state',{'key':key}).rows==[['current']]


@pytest.mark.parametrize('codec',['fp32','int8','binary','polar'])
def test_vector_selection_filters_before_shortlist(engine,monkeypatch,codec):
    from grag.config import EmbedderConfig
    from grag.retrieval import vectors
    from test_vectors import FakeEmbedder
    setup(engine)
    config=engine.config.model_copy(update={'embedder':EmbedderConfig(provider='fastembed',model='fake',dim=32),
                                           'vector_codec':codec,'max_embed_per_search':100})
    monkeypatch.setattr(vectors,'get_embedder',lambda config:FakeEmbedder())
    upsert_nodes(engine,config,UpsertNodesRequest(nodes=[
        UpsertNode(label='Note',key=f'old-{i}',properties={'body':'cache policy','status':'superseded'}) for i in range(40)
    ]+[UpsertNode(label='Note',key='new',properties={'body':'cache policy recent'})]))
    seeds=vectors.vector_candidates(engine,config,'cache policy',['Note'],1,evidence_now=dt.datetime.now(dt.timezone.utc))
    assert [s.node.id for s in seeds]==['Note:new']
    result=search_knowledge(engine,config,SearchRequest(query='cache policy',top_k=1,token_budget=3000))
    assert result.vector_status is None and result.included_node_ids==['Note:new']


def test_history_page_cursor_and_old_snapshot_survive_restart(tmp_path):
    from grag.config import GragConfig
    from grag.core.engine import Engine
    config=GragConfig(db_path=tmp_path/'history.lbdb',buffer_pool_size=128*1024**2)
    with Engine(config) as engine:
        setup(engine)
        write(engine,'policy','before',evidence=EvidenceUpdate(actor='first'))
        for i in range(24):
            write(engine,'policy',f'correction {i}',source=f'review-{i}.md')
    with Engine(config) as engine:
        seen=[]
        before=None
        while True:
            page=context(engine,'policy',history=True,history_before=before,token_budget=1000)
            assert page.response_token_estimate<=1000
            seen.extend(e.sequence for e in page.history.entries)
            before=page.history.next_before
            if before is None:
                break
        assert seen==list(range(25,0,-1))
        assert 'before' in context(engine,'policy',revision=1,token_budget=3000).context
        assert context(engine,'policy',history=True).history.entries[0].actor is None


@pytest.mark.parametrize('budget',[256,512,1000])
def test_historical_text_never_loses_its_qualifying_state(engine,budget):
    setup(engine)
    write(engine,'policy','obsolete claim '*150,source='old.md',evidence=EvidenceUpdate(state='retracted'))
    result=context(engine,'policy',evidence='all',token_budget=budget)
    assert result.response_token_estimate<=budget
    for n in result.subgraph.nodes:
        if 'body' in n.properties:
            assert n.properties['_evidence_state']=='retracted'
            assert n.properties['_evidence_visibility']=='retracted'
    # Pager either carries the state with exact text or refuses too-small budgets.
    try:
        page=context(engine,'policy',evidence='all',text_property='body',token_budget=budget)
    except GragError as exc:
        assert 'token_budget' in str(exc)
    else:
        assert '_evidence_state: "retracted"' in page.context
        assert page.response_token_estimate<=budget


def test_supersession_cycles_roll_back_all_nodes(engine):
    setup(engine)
    with pytest.raises(SchemaError,match='cycle'):
        upsert_nodes(engine,engine.config,UpsertNodesRequest(nodes=[
            UpsertNode(label='Note',key='a',evidence=EvidenceUpdate(state='superseded',superseded_by='Note:b')),
            UpsertNode(label='Note',key='b',evidence=EvidenceUpdate(state='superseded',superseded_by='Note:a'))]))
    assert engine.execute('MATCH (n:Note) RETURN count(n)').rows==[[0]]


def test_receipt_digest_preserves_old_requests_and_distinguishes_clear(engine):
    import hashlib

    from grag.core.mutations import OPERATIONS_TABLE
    from grag.core.revisions import canonical_json
    setup(engine)
    old_payload={'contract':'grag-upsert-v1','kind':'UpsertNodesRequest','request':{
        'nodes':[{'label':'Note','key':'old','properties':{'body':'legacy'},'source':None,'expected_revision':None}], 'edges':[]}}
    digest=hashlib.sha256(canonical_json(old_payload).encode()).hexdigest()
    engine.execute_write(f'CREATE NODE TABLE {OPERATIONS_TABLE}(id STRING PRIMARY KEY, digest STRING, result STRING)')
    engine.execute_write(f'CREATE (o:{OPERATIONS_TABLE} {{id:$id,digest:$digest,result:$result}})',
                         {'id':'legacy','digest':digest,'result':'{"nodes":1,"edges":0,"operation_id":"legacy"}'})
    replay=upsert_nodes(engine,engine.config,UpsertNodesRequest(operation_id='legacy',nodes=[UpsertNode(label='Note',key='old',properties={'body':'legacy'})]))
    assert replay.replayed
    request=UpsertNodesRequest(operation_id='new',nodes=[UpsertNode(label='Note',key='new',evidence=EvidenceUpdate())])
    upsert_nodes(engine,engine.config,request)
    request.nodes[0].evidence=EvidenceUpdate(expires_at=None)
    with pytest.raises(ConflictError,match='different request'):
        upsert_nodes(engine,engine.config,request)


@pytest.mark.parametrize('reverse',[False,True])
def test_supersession_checks_final_batch_state_independent_of_order(engine,reverse):
    setup(engine)
    write(engine,'b','replacement',evidence=EvidenceUpdate())
    write(engine,'a','original',evidence=EvidenceUpdate(state='superseded',superseded_by='Note:b'))
    patches=[UpsertNode(label='Note',key='a',expected_revision=content_revision(node(engine,'a')),
                       evidence=EvidenceUpdate(state='current',superseded_by=None)),
             UpsertNode(label='Note',key='b',expected_revision=content_revision(node(engine,'b')),
                       evidence=EvidenceUpdate(state='superseded',superseded_by='Note:a'))]
    if reverse:
        patches.reverse()
    upsert_nodes(engine,engine.config,UpsertNodesRequest(nodes=patches))
    assert context(engine,'a').included_node_ids==['Note:a']
    assert not context(engine,'b').included_node_ids


@pytest.mark.parametrize('status',['expired',' SUPERSEDED ','retracted'])
def test_adopting_legacy_history_does_not_resurrect_inactive_evidence(engine,status):
    setup(engine)
    upsert_nodes(engine,engine.config,UpsertNodesRequest(nodes=[UpsertNode(label='Note',key='old',properties={'status':status,'body':'old text'})]))
    write(engine,'old',expected_revision=content_revision(node(engine,'old')),evidence=EvidenceUpdate(actor='reviewer'))
    assert not context(engine,'old').included_node_ids
    result=search_knowledge(engine,engine.config,SearchRequest(query='old text',token_budget=3000))
    assert not result.included_node_ids


def test_paging_status_itself_keeps_exact_slice(engine):
    setup(engine)
    value='application-specific business state '*100
    upsert_nodes(engine,engine.config,UpsertNodesRequest(nodes=[UpsertNode(label='Note',key='state',properties={'status':value})]))
    result=context(engine,'state',text_property='status',text_offset=17,token_budget=1000)
    end=result.text_page.next_offset or len(value)
    assert result.subgraph.nodes[0].properties['status']==value[17:end]


@pytest.mark.parametrize('stage',['baseline','snapshot','commit'])
def test_crash_replay_keeps_history_graph_and_receipt_together(tmp_path,stage):
    import os
    import subprocess
    import sys

    from grag.config import GragConfig
    from grag.core.engine import Engine
    config=GragConfig(db_path=tmp_path/'crash.lbdb',buffer_pool_size=128*1024**2)
    with Engine(config) as engine:
        setup(engine)
        write(engine,'policy','stable policy',source='original.md')
        before=content_revision(node(engine,'policy'))
        search_knowledge(engine,config,SearchRequest(query='policy'))  # warm FTS before ALTER
    script='''
import os,sys
from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.mutate import upsert_nodes
from grag.core.types import UpsertNodesRequest,UpsertNode,EvidenceUpdate
engine=Engine(GragConfig(db_path=sys.argv[1],buffer_pool_size=128*1024**2))
original=engine.execute_write
def crash(query,params=None):
    result=original(query,params)
    if query.startswith('CREATE (h:_grag_evidence_history'):
        if (sys.argv[2]=='baseline' and params['seq']==0) or (sys.argv[2]=='snapshot' and params['seq']==1):
            os._exit(23)
    if sys.argv[2]=='commit' and query=='COMMIT':
        os._exit(23)
    return result
engine.execute_write=crash
upsert_nodes(engine,engine.config,UpsertNodesRequest(operation_id='crash-review',nodes=[UpsertNode(
    label='Note',key='policy',properties={'body':'corrected policy'},source='correction.md',
    expected_revision=sys.argv[3],evidence=EvidenceUpdate(actor='reviewer',reason='Source correction'))]))
'''
    child=subprocess.run(  # noqa: S603 — fixed crash worker on a disposable fixture
        [sys.executable,'-c',script,str(config.db_path),stage,before],
        env=os.environ.copy(),capture_output=True,timeout=40,check=False)
    assert child.returncode==23,child.stderr.decode()
    with Engine(config) as engine:
        assert node(engine,'policy')['body']==('corrected policy' if stage=='commit' else 'stable policy')
        retry=upsert_nodes(engine,config,UpsertNodesRequest(operation_id='crash-review',nodes=[UpsertNode(
            label='Note',key='policy',properties={'body':'corrected policy'},source='correction.md',
            expected_revision=before,evidence=EvidenceUpdate(actor='reviewer',reason='Source correction'))]))
        assert retry.replayed==(stage=='commit')
        assert node(engine,'policy')['_evidence_seq']==1
        history=context(engine,'policy',history=True,token_budget=3000).history
        assert [e.sequence for e in history.entries]==[1,0]
        assert history.entries[-1].source=='original.md'


@pytest.mark.parametrize('sections',[False,True],ids=['flat','section-ownership'])
def test_document_lifecycle_schema_survives_abrupt_committed_restart(tmp_path,sections):
    import os
    import subprocess
    import sys

    from grag.config import GragConfig
    from grag.core.engine import Engine
    config=GragConfig(db_path=tmp_path/'docs.lbdb',buffer_pool_size=128*1024**2)
    script='''
import os,sys
from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.types import IngestRequest,IngestDocument
from grag.ingest.loaders import ingest_documents
e=Engine(GragConfig(db_path=sys.argv[1],buffer_pool_size=128*1024**2))
ingest_documents(e,e.config,IngestRequest(documents=[IngestDocument(text='# Policy\\n\\nCache for fifteen seconds.',source='policy.md')],sections=sys.argv[2]=='1'))
os._exit(23)
'''
    child=subprocess.run(  # noqa: S603 — fixed crash worker on a disposable fixture
        [sys.executable,'-c',script,str(config.db_path),'1' if sections else '0'],
        env=os.environ.copy(),capture_output=True,timeout=40,check=False)
    assert child.returncode==23,child.stderr.decode()
    with Engine(config) as engine:
        assert engine.execute('MATCH (n:Chunk) RETURN n._document_state').rows==[['current']]
        result=search_knowledge(engine,config,SearchRequest(query='fifteen seconds',token_budget=4000))
        assert 'fifteen seconds' in result.context
        if sections:
            assert engine.execute('MATCH ()-[r:IN_SECTION]->() RETURN count(r)').rows==[[1]]
