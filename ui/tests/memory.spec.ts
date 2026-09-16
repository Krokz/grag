import {test,expect, type APIRequestContext, type Page} from '@playwright/test';
import {readFile} from 'node:fs/promises';

const id='Use local storage';
const cypher="MATCH (n:Decision {name:'Use local storage'}) RETURN n";
async function current(request:APIRequestContext,db='alpha') {
  const response=await request.post(`/api/query?db=${db}`,{data:{cypher}});
  expect(response.ok()).toBeTruthy();return (await response.json()).rows[0][0];
}
async function edit(request:APIRequestContext,summary:string,reason:string,db='alpha') {
  const node=await current(request,db);
  const response=await request.post(`/api/nodes/upsert?db=${db}`,{data:{nodes:[{label:'Decision',key:id,
    expected_revision:node._revision,properties:{summary},evidence:{actor:'Browser fixture',reason,state:'current',review:'unreviewed',expires_at:null,superseded_by:null}}]}});
  expect(response.ok()).toBeTruthy();
}
async function selectDecision(page:Page) {
  await page.getByLabel('Memory type').selectOption('Decision');
  await page.getByRole('button',{name:/Use local storage/}).click();
  await expect(page.getByRole('button',{name:'Edit memory',exact:true})).toBeVisible();
}
test.beforeEach(async({page,request})=>{
  await edit(request,'alpha context stays on this machine.','Reset fixture');
  const sample=page.waitForResponse(response=>response.url().includes('/api/graph/sample'));
  await page.goto('/');
  const overview=await (await sample).json();
  await expect(page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Graph',exact:true})).toHaveAttribute('aria-current','page');
  await expect(page.locator('.canvas-toolbar')).toContainText(`${overview.subgraph.nodes.length} nodes`);
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Memories',exact:true}).click();
  await expect(page.getByRole('heading',{name:'What this project remembers'})).toBeVisible();
});

test('browse, custom keys, source-only empty state and task conventions',async({page})=>{
  await page.getByRole('button',{name:'Open tasks',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Investigate duplicate events'})).toBeVisible();
  await expect(page.getByRole('heading',{name:'Document setup'})).toHaveCount(0);
  await page.getByRole('button',{name:'Memories',exact:true}).last().click();
  await page.getByLabel('Memory type').selectOption('Finding');
  await page.getByRole('button',{name:/Numeric primary keys/}).click();
  await expect(page.getByRole('article')).toContainText('Finding:42');
  await page.getByLabel('Selected database').selectOption('sources');
  await expect(page.getByRole('heading',{name:'No authored memories found'})).toBeVisible();
  await page.getByLabel('Memory type').selectOption('Module');
  await page.getByRole('button',{name:/worker/}).click();
  await expect(page.getByRole('heading',{name:'Analysis coverage'})).toBeVisible();
  await expect(page.locator('.memory-card')).toContainText('Indexed source');
  await expect(page.locator('.memory-detail .badges')).toContainText('Indexed source');
  await expect(page.locator('.memory-detail .badges')).not.toContainText('unreviewed');
  await expect(page.locator('.memory-detail .badges')).not.toContainText('untracked');
  await expect(page.getByRole('button',{name:'Edit memory',exact:true})).toHaveCount(0);
  await page.getByText('Unresolved sites and limits',{exact:true}).click();
  await expect(page.locator('.coverage li')).toContainText('Synthetic unresolved target');
  await page.screenshot({path:'test-results/coverage.png',fullPage:true});
});

test('edit, inspect evidence/history and follow a stored relationship',async({page,request})=>{
  await selectDecision(page);
  await expect(page.locator('.memory-detail .badges')).toContainText('unreviewed');
  await page.getByRole('button',{name:'Load linked evidence'}).click();
  await expect(page.getByRole('button',{name:/Module worker SUPPORTED_BY/})).toBeVisible();
  await page.getByRole('button',{name:'Edit memory',exact:true}).click();
  await page.getByLabel('summary',{exact:true}).fill('Keep local memory and portable backups.');
  await page.getByLabel('Reason for this change').fill('Backups are now part of the decision.');
  await page.getByRole('button',{name:'Save change',exact:true}).click();
  await expect(page.getByRole('status').filter({hasText:'Saved.'})).toBeVisible();
  const stored=await current(request);expect(stored.summary).toBe('Keep local memory and portable backups.');
  expect(stored._source).toBe('docs/architecture.md');
  await page.getByRole('tab',{name:'History',exact:true}).click();
  await expect(page.getByText('Backups are now part of the decision.',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:`View revision ${stored._evidence_seq}`,exact:true}).click();
  await expect(page.locator('.snapshot')).toContainText(stored.summary);
  await page.screenshot({path:'test-results/history.png',fullPage:true});
});

test('conflict keeps draft and never overwrites another writer',async({page,request})=>{
  await selectDecision(page);await page.getByRole('button',{name:'Edit memory',exact:true}).click();
  await page.getByLabel('summary',{exact:true}).fill('My pending UI edit');
  await page.getByLabel('Reason for this change').fill('Trying to correct this');
  await edit(request,'A different agent already corrected this.','Concurrent update');
  await page.getByRole('button',{name:'Save change',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('changed in another session');
  await expect(page.getByLabel('summary',{exact:true})).toHaveValue('My pending UI edit');
  expect((await current(request)).summary).toBe('A different agent already corrected this.');
  await page.getByRole('button',{name:'Reload latest (discard draft)'}).click();
  await expect(page.getByRole('article')).toContainText('A different agent already corrected this.');
});

test('lost write response retries the identical operation once',async({page,request})=>{
  await selectDecision(page);const before=await current(request);
  const bodies:string[]=[];
  await page.route('**/api/nodes/upsert*',async route=>{
    bodies.push(route.request().postData()!);
    if(bodies.length===1) {await route.fetch();await route.abort('failed');}
    else await route.continue();
  });
  await page.getByRole('button',{name:'Edit memory',exact:true}).click();
  await page.getByLabel('summary',{exact:true}).fill('A single durable correction.');
  await page.getByLabel('Reason for this change').fill('Exercise response loss');
  await page.getByRole('button',{name:'Save change',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('outcome is unconfirmed');
  await page.getByRole('button',{name:'Retry same save',exact:true}).click();
  await expect(page.getByRole('status').filter({hasText:'Saved.'})).toBeVisible();
  expect(bodies).toHaveLength(2);expect(bodies[1]).toBe(bodies[0]);
  const after=await current(request);expect(after._evidence_seq).toBe(before._evidence_seq+1);
});

test('retirement preserves content and history, restore is explicit',async({page,request})=>{
  await selectDecision(page);const before=await current(request);
  await page.getByRole('button',{name:'Retire memory',exact:true}).click();
  await page.getByLabel('Reason for this change').fill('No longer applicable');
  await page.getByRole('button',{name:'Save change',exact:true}).click();
  await expect(page.getByRole('button',{name:'Restore memory',exact:true})).toBeVisible();
  const retired=await current(request);expect(retired._evidence_state).toBe('retracted');expect(retired.summary).toBe(before.summary);
  await expect(page.getByRole('heading',{name:'No matching records'})).toBeVisible();
  await page.getByLabel('Include inactive evidence').check();
  await expect(page.locator('.memory-card')).toContainText('retracted');
  await page.getByRole('button',{name:'Restore memory',exact:true}).click();
  await page.getByLabel('Reason for this change').fill('Applicable again');
  await page.getByRole('button',{name:'Save change',exact:true}).click();
  await expect(page.getByRole('button',{name:'Retire memory',exact:true})).toBeVisible();
  expect((await current(request))._evidence_state).toBe('current');
});

test('database switch drops a delayed response and clears the old editor',async({page,request})=>{
  await selectDecision(page);await page.getByRole('button',{name:'Edit memory',exact:true}).click();
  await page.getByLabel('summary',{exact:true}).fill('Never write this to beta');
  let release:()=>void=()=>{};
  const waiting=new Promise<void>(resolve=>{release=resolve;});
  await page.route('**/api/memories*',async route=>{
    if(route.request().url().includes('db=alpha')) {const response=await route.fetch();await waiting;await route.fulfill({response});}
    else await route.continue();
  });
  await page.getByRole('button',{name:'Refresh memories',exact:true}).click();
  await page.getByLabel('Selected database').selectOption('beta');release();
  await expect(page.getByLabel('summary',{exact:true})).toHaveCount(0);
  await selectDecision(page);
  await expect(page.getByRole('article')).toContainText('beta context stays on this machine.');
  expect((await current(request,'beta')).summary).toBe('beta context stays on this machine.');
});

test('search, review, recent changes, health and responsive layout',async({page,request})=>{
  await page.getByLabel('Search memories').fill('queries available offline');
  await page.getByRole('button',{name:'Find',exact:true}).click();
  await expect(page.locator('.memory-card')).toHaveCount(1);
  await selectDecision(page);await page.getByRole('button',{name:'Review',exact:true}).click();
  await page.getByLabel('Review outcome').selectOption('accepted');
  await page.getByLabel('Reason for this change').fill('Checked against the architecture document');
  await page.getByRole('button',{name:'Save change',exact:true}).click();
  await expect(page.getByRole('status').filter({hasText:'Saved.'})).toBeVisible();
  expect((await current(request))._review_state).toBe('accepted');
  await page.getByRole('button',{name:'Recent changes',exact:true}).click();
  await expect(page.locator('.memory-card').first()).toContainText('Changed');
  await page.screenshot({path:'test-results/memories.png',fullPage:true});
  await page.setViewportSize({width:620,height:850});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();
  await page.screenshot({path:'test-results/memories-mobile.png',fullPage:true});
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Health',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Index & ingestion health'})).toBeVisible();
  await expect(page.getByText('No background embedding worker is active.',{exact:false})).toBeVisible();
});

test('graph handoff preserves inspection and full SVG export',async({page,request})=>{
  await selectDecision(page);
  await page.getByRole('button',{name:'Explore relationships',exact:true}).click();
  await expect(page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Graph',exact:true})).toHaveAttribute('aria-current','page');
  await page.getByRole('button',{name:'Expand neighbors',exact:true}).click();
  await expect(page.locator('.canvas-toolbar')).toContainText('1 edges');
  const downloadPromise=page.waitForEvent('download');
  await page.getByRole('button',{name:'Export full SVG',exact:true}).click();
  const download=await downloadPromise;
  expect(await download.failure()).toBeNull();
  const svg=await readFile((await download.path())!,'utf8');
  const topology=await (await request.get('/api/graph/export?db=alpha')).json();
  expect((svg.match(/<circle /g)||[]).length).toBe(topology.stats.node_count);
  expect((svg.match(/<line /g)||[]).length).toBe(topology.stats.edge_count);
  await page.getByRole('button',{name:'Evidence & history',exact:true}).click();
  await expect(page.getByRole('article')).toContainText('Decision:Use local storage');
});

test('editing a dependency updates the cached graph without a page reload',async({page,request})=>{
  await page.getByLabel('Memory type').selectOption('Dependency');
  await page.getByRole('button',{name:/ladybug/}).click();
  await page.getByRole('button',{name:'Explore relationships',exact:true}).click();
  await expect(page.locator('.inspector .kv')).toContainText('0.20.1');
  const topology=(await page.locator('.canvas-toolbar').innerText()).match(/\d+ nodes · \d+ edges/)![0];
  await page.getByRole('button',{name:'Evidence & history',exact:true}).click();
  await page.getByRole('button',{name:'Edit memory',exact:true}).click();
  await page.getByLabel('version',{exact:true}).fill('0.20.3');
  await page.getByLabel('Reason for this change').fill('Match the supported runtime version');
  await page.getByRole('button',{name:'Save change',exact:true}).click();
  await expect(page.getByRole('status').filter({hasText:'Saved.'})).toBeVisible();
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Graph',exact:true}).click();
  await expect(page.locator('.inspector .kv')).toContainText('0.20.3');
  await expect(page.locator('.inspector .kv')).not.toContainText('0.20.1');
  await expect(page.locator('.canvas-toolbar')).toContainText(topology);
  const stored=await request.post('/api/query?db=alpha',{data:{cypher:"MATCH (d:Dependency {name:'ladybug'}) RETURN d.version"}});
  expect((await stored.json()).rows).toEqual([['0.20.3']]);
});

test('a save finishing after returning to Graph still updates its node and edges',async({page})=>{
  await selectDecision(page);
  await page.getByRole('button',{name:'Explore relationships',exact:true}).click();
  await page.getByRole('button',{name:'Expand neighbors',exact:true}).click();
  await expect(page.locator('.canvas-toolbar')).toContainText('1 edges');
  const topology=(await page.locator('.canvas-toolbar').innerText()).match(/\d+ nodes · \d+ edges/)![0];
  await page.getByRole('button',{name:'Evidence & history',exact:true}).click();
  let release:()=>void=()=>{};let committed:()=>void=()=>{};
  const waiting=new Promise<void>(resolve=>{release=resolve;});
  const saved=new Promise<void>(resolve=>{committed=resolve;});
  await page.route('**/api/nodes/upsert*',async route=>{
    const response=await route.fetch();committed();await waiting;await route.fulfill({response});
  });
  await page.getByRole('button',{name:'Edit memory',exact:true}).click();
  await page.getByLabel('summary',{exact:true}).fill('Updated while switching views.');
  await page.getByLabel('Reason for this change').fill('Keep cached graph records in sync');
  await page.getByRole('button',{name:'Save change',exact:true}).click();
  await saved;
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Graph',exact:true}).click();
  release();
  await expect(page.locator('.inspector .kv')).toContainText('Updated while switching views.');
  await expect(page.locator('.canvas-toolbar')).toContainText(topology);
});

test('expanding a cached node replaces old properties with the latest stored values',async({page,request})=>{
  await selectDecision(page);
  await page.getByRole('button',{name:'Explore relationships',exact:true}).click();
  await expect(page.locator('.inspector .kv')).toContainText('alpha context stays on this machine.');
  await edit(request,'Updated by a connected agent.','Agent changed the stored memory');
  await page.getByRole('button',{name:'Expand neighbors',exact:true}).click();
  await expect(page.locator('.inspector .kv')).toContainText('Updated by a connected agent.');
  await expect(page.locator('.inspector .kv')).not.toContainText('alpha context stays on this machine.');
});

test('a late save from another database cannot update the selected graph',async({page,request})=>{
  await selectDecision(page);
  let release:()=>void=()=>{};let committed:()=>void=()=>{};
  const waiting=new Promise<void>(resolve=>{release=resolve;});
  const saved=new Promise<void>(resolve=>{committed=resolve;});
  await page.route('**/api/nodes/upsert*',async route=>{
    const response=await route.fetch();committed();await waiting;await route.fulfill({response});
  });
  await page.getByRole('button',{name:'Edit memory',exact:true}).click();
  await page.getByLabel('summary',{exact:true}).fill('Only alpha was edited.');
  await page.getByLabel('Reason for this change').fill('Do not update another database view');
  await page.getByRole('button',{name:'Save change',exact:true}).click();
  await saved;
  await page.getByLabel('Selected database').selectOption('beta');
  await selectDecision(page);
  await page.getByRole('button',{name:'Explore relationships',exact:true}).click();
  await expect(page.locator('.inspector .kv')).toContainText('beta context stays on this machine.');
  const response=page.waitForResponse(r=>r.url().includes('/api/nodes/upsert'));
  release();await (await response).finished();
  await page.evaluate(()=>new Promise<void>(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve()))));
  await expect(page.locator('.inspector .kv')).toContainText('beta context stays on this machine.');
  expect((await current(request,'alpha')).summary).toBe('Only alpha was edited.');
  expect((await current(request,'beta')).summary).toBe('beta context stays on this machine.');
});

test('a graph read started before a save cannot restore its old properties',async({page})=>{
  await selectDecision(page);
  await page.getByRole('button',{name:'Explore relationships',exact:true}).click();
  let release:()=>void=()=>{};let captured:()=>void=()=>{};
  const waiting=new Promise<void>(resolve=>{release=resolve;});
  const started=new Promise<void>(resolve=>{captured=resolve;});
  await page.route('**/api/query*',async route=>{
    if(route.request().postDataJSON().cypher.includes('OPTIONAL MATCH')) {
      const response=await route.fetch();captured();await waiting;await route.fulfill({response});
    } else await route.continue();
  });
  await page.getByRole('button',{name:'Expand neighbors',exact:true}).click();
  await started;
  await page.getByRole('button',{name:'Evidence & history',exact:true}).click();
  await page.getByRole('button',{name:'Edit memory',exact:true}).click();
  await page.getByLabel('summary',{exact:true}).fill('The confirmed edit stays visible.');
  await page.getByLabel('Reason for this change').fill('Ignore the older graph response');
  await page.getByRole('button',{name:'Save change',exact:true}).click();
  await expect(page.getByRole('status').filter({hasText:'Saved.'})).toBeVisible();
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Graph',exact:true}).click();
  const response=page.waitForResponse(r=>r.url().includes('/api/query') && r.request().postDataJSON().cypher.includes('OPTIONAL MATCH'));
  release();await (await response).finished();
  await page.evaluate(()=>new Promise<void>(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve()))));
  await expect(page.locator('.inspector .kv')).toContainText('The confirmed edit stays visible.');
});

test('graph index badge follows passive verification without reloading the graph',async({page})=>{
  await expect(page.getByLabel('Memory type').locator('option[value="Decision"]')).toHaveCount(1);
  let state='checking';let samples=0;
  const observedUrls:string[]=[];
  await page.route('**/api/graph/sample*',async route=>{
    samples++;const response=await route.fetch();const body=await response.json();
    await route.fulfill({response,json:{...body,freshness:{status:'checking',checked_at:null,timed_out:false}}});
  });
  await page.route('**/api/index/status*',async route=>{
    observedUrls.push(route.request().url());
    await route.fulfill({json:{observed_only:true,loaded:true,database_id:'alpha',index:{running:state==='checking',freshness:{status:state,checked_at:state==='fresh'?'2026-09-14T12:00:00Z':null,timed_out:false}}}});
  });
  await page.reload();
  const badge=page.getByLabel('Code index status',{exact:true});
  await expect(badge).toHaveText('index: checking');
  await expect(badge).toHaveAttribute('title',/Last graph\/schema read: checking/);
  state='fresh';
  await expect(badge).toHaveText('index: fresh');
  await expect(badge).toHaveAttribute('title',/Last graph\/schema read: checking/);
  expect(samples).toBe(1);
  expect(observedUrls.every(url=>new URL(url).searchParams.get('check')==='false')).toBeTruthy();
});

test('failed passive status cannot leave an old fresh badge',async({page})=>{
  await page.clock.install();
  let fail=false;
  await page.route('**/api/index/status*',async route=>{
    if(fail) await route.fulfill({status:503,json:{error:'Status observation unavailable'}});
    else await route.fulfill({json:{observed_only:true,loaded:true,index:{running:false,freshness:{status:'fresh',checked_at:'2026-09-14T12:00:00Z',timed_out:false}}}});
  });
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Graph',exact:true}).click();
  const badge=page.getByLabel('Code index status',{exact:true});
  await expect(badge).toHaveText('index: fresh');
  fail=true;await page.clock.fastForward(16000);
  await expect(badge).toHaveText('index: unavailable');
  await expect(badge).toHaveAttribute('title',/Status observation unavailable/);
});

test('database switch cancels the old index observation',async({page})=>{
  let release:()=>void=()=>{};let started:()=>void=()=>{};
  const waiting=new Promise<void>(resolve=>{release=resolve;});
  const captured=new Promise<void>(resolve=>{started=resolve;});
  const observedUrls:string[]=[];
  await page.route('**/api/index/status*',async route=>{
    observedUrls.push(route.request().url());
    const alpha=new URL(route.request().url()).searchParams.get('db')==='alpha';
    if(alpha) {started();await waiting;}
    await route.fulfill({json:{observed_only:true,loaded:true,index:{running:false,freshness:{status:alpha?'fresh':'error',checked_at:null,timed_out:false},last_error:alpha?null:'Beta source is unavailable'}}});
  });
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Graph',exact:true}).click();
  await captured;
  await page.getByLabel('Selected database').selectOption('beta');
  await expect(page.getByLabel('Code index status',{exact:true})).toHaveText('index: error');
  release();
  await page.evaluate(()=>new Promise<void>(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve()))));
  await expect(page.getByLabel('Code index status',{exact:true})).toHaveText('index: error');
  await expect(page.getByLabel('Code index status',{exact:true})).toHaveAttribute('title',/Beta source is unavailable/);
  expect(observedUrls.some(url=>new URL(url).searchParams.get('db')==='beta')).toBeTruthy();
});

test('history pages older entries and list failures stay errors',async({page,request})=>{
  for(let i=0;i<22;i++) await edit(request,`History fixture ${i}`,`Recorded check ${i}`);
  await selectDecision(page);
  await page.getByRole('tab',{name:'History',exact:true}).click();
  await expect(page.getByText('Recorded check 21',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Older revisions',exact:true}).click();
  await expect(page.getByText('Recorded check 0',{exact:true})).toBeVisible();
  await page.route('**/api/memories*',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Fixture owner unavailable'})}));
  await page.getByRole('button',{name:'Refresh memories',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('Fixture owner unavailable');
  await expect(page.getByRole('heading',{name:'No authored memories found'})).toHaveCount(0);
});

test('a late revision response cannot replace the latest selection',async({page,request})=>{
  await edit(request,'Earlier snapshot marker.','Earlier snapshot');
  const earlier=await current(request);
  await edit(request,'Latest snapshot marker.','Latest snapshot');
  const latest=await current(request);
  await selectDecision(page);
  await page.getByRole('tab',{name:'History',exact:true}).click();
  let release:()=>void=()=>{};
  let captured:()=>void=()=>{};
  const waiting=new Promise<void>(resolve=>{release=resolve;});
  const started=new Promise<void>(resolve=>{captured=resolve;});
  await page.route('**/api/context*',async route=>{
    if(route.request().postDataJSON().revision===earlier._evidence_seq) {
      const response=await route.fetch();captured();await waiting;await route.fulfill({response});
    } else await route.continue();
  });
  await page.getByRole('button',{name:`View revision ${earlier._evidence_seq}`,exact:true}).click();
  await started;
  await page.getByRole('button',{name:`View revision ${latest._evidence_seq}`,exact:true}).click();
  await expect(page.locator('.snapshot')).toContainText('Latest snapshot marker.');
  const oldResponse=page.waitForResponse(response=>response.url().includes('/api/context') && response.request().postDataJSON().revision===earlier._evidence_seq);
  release();await (await oldResponse).finished();
  await page.evaluate(()=>new Promise<void>(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve()))));
  await expect(page.locator('.snapshot')).toContainText('Latest snapshot marker.');
  await expect(page.locator('.snapshot')).not.toContainText('Earlier snapshot marker.');
});

test('retiring the only record on the last page returns to a valid page',async({page,request})=>{
  const schema=await request.post('/api/schema/define?db=alpha',{data:{node_tables:[{name:'PageNote',properties:[{name:'id'},{name:'title'},{name:'body'}]}]}});
  expect(schema.ok()).toBeTruthy();
  const write=await request.post('/api/nodes/upsert?db=alpha',{data:{nodes:Array.from({length:31},(_,i)=>({label:'PageNote',key:`page-${i}`,properties:{title:`Page item ${String(i).padStart(2,'0')}`,body:'Pagination fixture'}}))}});
  expect(write.ok()).toBeTruthy();
  await page.getByRole('button',{name:'Refresh memories',exact:true}).click();
  await page.getByLabel('Memory type').selectOption('PageNote');
  await expect(page.locator('.memory-card')).toHaveCount(30);
  await page.getByRole('button',{name:'Next',exact:true}).click();
  await expect(page.locator('.memory-card')).toHaveCount(1);
  await page.locator('.memory-card').click();
  await page.getByRole('button',{name:'Retire memory',exact:true}).click();
  await page.getByLabel('Reason for this change').fill('Remove last-page item from current evidence');
  await page.getByRole('button',{name:'Save change',exact:true}).click();
  await expect(page.locator('.memory-card')).toHaveCount(30);
  await expect(page.locator('.pagination')).toContainText('1–30 of 30');
  await expect(page.getByRole('heading',{name:'No authored memories found'})).toHaveCount(0);
});

test('an explicit dispute on indexed source stays visible',async({page,request})=>{
  const read=await request.post('/api/query?db=sources',{data:{cypher:"MATCH (n:Module {id:'worker.go'}) RETURN n"}});
  const node=(await read.json()).rows[0][0];
  const write=await request.post('/api/nodes/upsert?db=sources',{data:{nodes:[{label:'Module',key:node.id,expected_revision:node._revision,
    evidence:{review:'disputed',actor:'Browser fixture',reason:'Check disputed parser evidence'}}]}});
  expect(write.ok()).toBeTruthy();
  await page.getByLabel('Selected database').selectOption('sources');
  await page.getByLabel('Memory type').selectOption('Module');
  await page.getByLabel('Include inactive evidence').check();
  await page.getByRole('button',{name:/worker/}).click();
  await expect(page.locator('.memory-detail .badges')).toContainText('Indexed source');
  await expect(page.locator('.memory-detail .badges')).toContainText('disputed');
  await expect(page.getByRole('button',{name:'Edit memory',exact:true})).toHaveCount(0);
});
