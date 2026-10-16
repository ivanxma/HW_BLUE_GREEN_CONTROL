'use strict';
const app = document.querySelector('#app');
const csrf = app.dataset.csrf, demo = app.dataset.demo === 'true';
const mutationsDisabled = demo || app.dataset.readOnly === 'true';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let inventory, selected, fleet = 'standalone', descending = false, detailData, refreshing = false;
const tone = status => ['FAILED','INTERRUPTED','SWITCHOVER_FAILED'].includes(status) ? 'red' : ['ACTIVE','SUCCEEDED','NOT_STARTED','READY_FOR_SWITCHOVER','SWITCHOVER_COMPLETED','INACTIVE'].includes(status) ? 'green' : 'orange';
const badge = (label, color) => `<span class="badge ${color || tone(label)}"><i class="dot"></i>${esc(label)}</span>`;
const field = (label, name, value = '', type = 'text', required = false) => `<label>${esc(label)}${required ? ' *' : ''}<input name="${name}" value="${esc(value)}" type="${type}" ${required ? 'required' : ''}></label>`;
const select = (label, name, options, value = '') => `<label>${esc(label)}<select name="${name}" aria-label="${esc(label)}">${options.map(o => `<option value="${esc(o[0])}" ${o[0] === value ? 'selected' : ''}>${esc(o[1])}</option>`).join('')}</select></label>`;
const check = (label, name, checked = false) => `<label class="checkbox-field"><input type="checkbox" name="${name}" ${checked ? 'checked' : ''}>${esc(label)}</label>`;
const pretty = x => esc(JSON.stringify(x, null, 2));
const time = x => x ? new Date(x * 1000).toISOString().replace('T',' ').slice(0,19) + ' UTC' : 'In progress';
const duration = (start,end) => `${Math.max(0,Math.round((end || Date.now()/1000)-start))}s`;

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});
  if (response.status === 401) { location.href='/login'; throw Error('Session expired'); }
  const data = await response.json();
  if (!response.ok) throw Error(data.error || 'Request failed');
  return data;
}
// Keep existing DOM nodes when the displayed value has not changed.
const renderedHTML=new WeakMap();
let uiRevision=0;
function setProperty(node, property, value) {
 if(typeof node==='string')node=$(node);
 if(node && node[property]!==value){node[property]=value;uiRevision++;return true;}return false;
}
function setText(node,value){return setProperty(node,'textContent',String(value??''));}
function setHTML(node,content) {
 if(typeof node==='string')node=$(node);
 if(!node || renderedHTML.get(node)===content)return false;
 const detailKey=(d,i)=>d.dataset.detailKey||`${d.closest('[data-resource-id]')?.dataset.resourceId||''}:${d.querySelector('summary')?.textContent||''}:${i}`;
 const opened=new Set([...node.querySelectorAll('details')].flatMap((d,i)=>d.open?[detailKey(d,i)]:[]));
 const scroll=[node,...node.querySelectorAll('pre,.table-wrap')].map(n=>({top:n.scrollTop,left:n.scrollLeft}));
 const focus=node.contains(document.activeElement)?document.activeElement:null;
 const attributes=focus?[...focus.attributes].filter(a=>a.name==='id'||a.name==='name'||a.name.startsWith('data-')).map(a=>`[${a.name}="${CSS.escape(a.value)}"]`).join(''):'';
 node.innerHTML=content;renderedHTML.set(node,content);uiRevision++;
 [...node.querySelectorAll('details')].forEach((d,i)=>{if(opened.has(detailKey(d,i)))d.open=true;});
 [node,...node.querySelectorAll('pre,.table-wrap')].forEach((n,i)=>{if(scroll[i]){n.scrollTop=scroll[i].top;n.scrollLeft=scroll[i].left;}});
 if(attributes)node.querySelector(attributes)?.focus({preventScroll:true});
 return true;
}
function stampChanges(previous){if(uiRevision!==previous)setText('#refreshed',`Last change ${new Date().toLocaleTimeString()} · checks every 15s`);}

const requestPages=new Map(),requestViews=new Map();
function renderWorkRequests(target,requests,key) {
 const node=typeof target==='string'?$(target):target;if(!node)return;
 requestViews.set(node.id,{target:'#'+node.id,requests,key});
 const state=requestPages.get(key)||{page:1,size:10};requestPages.set(key,state);
 const pages=Math.max(1,Math.ceil(requests.length/state.size));state.page=Math.min(pages,Math.max(1,state.page));
 const start=(state.page-1)*state.size, shown=requests.slice(start,start+state.size);
 setHTML(node,`<div class="request-page">${shown.map(w=>`<div class="request"><div><strong>${esc(w.operation_type)}</strong><span class="small muted">${esc(w.percent_complete??0)}% · ${esc(w.time_started||w.time_accepted||'')}</span></div>${badge(w.status)}<button data-work="${esc(w.id)}">Details</button></div>`).join('')||'<p class="small muted">No retained work requests for this selection.</p>'}</div><div class="pagination" aria-label="Work request pagination"><label>Rows per page<select data-page-size="${esc(key)}" aria-label="${esc(node.id)} rows per page">${[10,25,50].map(n=>`<option ${state.size===n?'selected':''}>${n}</option>`).join('')}</select></label><span class="small muted">${requests.length?start+1:0}–${Math.min(start+state.size,requests.length)} of ${requests.length} · Page ${state.page} of ${pages}</span><div><button data-work-page="previous" data-page-key="${esc(key)}" ${state.page===1?'disabled':''}>Previous</button><button data-work-page="next" data-page-key="${esc(key)}" ${state.page===pages?'disabled':''}>Next</button></div></div>`);
}
function redrawRequestPages(key){for(const view of requestViews.values())if(view.key===key)renderWorkRequests(view.target,view.requests,key);}

const systemTabs=new Map();
function systemKey(id=inspected){return `${inventory.compartment_id}:${id}`;}
function activateDetailTab(root,name,focus=false,load=true) {
 if(!root)return;systemTabs.set(root.dataset.tabKey,name);
 root.querySelectorAll('[data-detail-tab]').forEach(button=>{
  const active=button.dataset.detailTab===name;
  if(button.classList.contains('active')!==active)button.classList.toggle('active',active);
  setProperty(button,'ariaSelected',String(active));
  setProperty(button,'tabIndex',active?0:-1);
  setProperty('#'+button.getAttribute('aria-controls'),'hidden',!active);
  if(active&&focus)button.focus();
 });
 if(load)void loadSqlTab(root,name);
}
function activateSystemTab(name,focus=false){activateDetailTab($('#db-tabview'),name,focus);}
const detailTabNames=[['details','Details'],['replicas','Replicas'],['replication','Replication'],['work','Work Request'],['variables','Variables'],['status','Status'],['configuration','OCI Configuration']];
function detailTabMarkup(prefix,label,key,system=false) {
 const content={details:`<div id="${prefix}-connection"></div>`,replicas:`<div id="${prefix}-replicas"></div>`,
  replication:`<div id="${prefix}-channels"></div><div id="${prefix}-gtids"></div>`,
  work:`<div id="${prefix}-work-scope"></div><div id="${prefix}-work-requests"></div>`,variables:`<div id="${prefix}-variables" data-sql-table="variables"></div>`,status:`<div id="${prefix}-status" data-sql-table="status"></div>`,configuration:`<div id="${prefix}-configuration"></div>`};
 return `<div id="${prefix}-tabview" data-tabview data-tab-key="${esc(key)}"><div class="detail-tabs inventory-tabs" role="tablist" aria-label="${esc(label)}">${detailTabNames.map(([id,title])=>`<button type="button" role="tab" class="tab" data-detail-tab="${id}" ${system?`data-system-tab="${id}"`:''} id="${prefix}-tab-${id}" aria-controls="${prefix}-view-${id}" aria-selected="false" tabindex="-1">${title}</button>`).join('')}</div>${detailTabNames.map(([id])=>`<section id="${prefix}-view-${id}" class="db-tab-panel" role="tabpanel" aria-labelledby="${prefix}-tab-${id}" hidden>${content[id]}</section>`).join('')}</div>`;
}
const sqlStates=new Map(),sqlViews=new WeakMap();
function sqlState(root,kind){
 const key=root.dataset.tabKey+':'+kind;
 if(!sqlStates.has(key))sqlStates.set(key,{rows:null,error:'',search:'',page:1,size:25,generation:0,pending:null});
 return sqlStates.get(key);
}
function renderSqlTable(root,kind){
 const node=root.querySelector(`[data-sql-table="${kind}"]`);if(!node)return;
 const state=sqlState(root,kind),title=kind==='variables'?'Variables':'Status';
 if(!node.firstElementChild){
  setHTML(node,`<h3>Global ${title.toLowerCase()}</h3><p class="small muted">Live SQL · SHOW GLOBAL ${kind.toUpperCase()} · Search names and values</p><div class="sql-table-toolbar"><label for="${node.id}-search">Search ${title.toLowerCase()}<input type="search" id="${node.id}-search" data-sql-search="${kind}" placeholder="Name or value"></label><label>Rows per page<select data-sql-size="${kind}" aria-label="${title} rows per page">${[10,25,50,100].map(n=>`<option value="${n}">${n}</option>`).join('')}</select></label></div><div data-sql-information role="status"></div><div class="table-wrap sql-values"><table aria-label="Global ${title.toLowerCase()}"><thead><tr><th scope="col">Name</th><th scope="col">Value</th></tr></thead><tbody></tbody></table></div><div class="pagination"><span class="small muted" data-sql-page-label></span><div><button data-sql-page="previous" data-sql-kind="${kind}">Previous</button><button data-sql-page="next" data-sql-kind="${kind}">Next</button></div></div>`);
 }
 setProperty(node.querySelector('[data-sql-search]'),'value',state.search);
 setProperty(node.querySelector('[data-sql-size]'),'value',String(state.size));
 const query=state.search.toLocaleLowerCase(),rows=(state.rows||[]).filter(row=>`${row.name} ${row.value}`.toLocaleLowerCase().includes(query));
 const pages=Math.max(1,Math.ceil(rows.length/state.size));state.page=Math.max(1,Math.min(state.page,pages));
 const start=(state.page-1)*state.size;
 setHTML(node.querySelector('tbody'),rows.slice(start,start+state.size).map(row=>`<tr><th scope="row">${esc(row.name)}</th><td>${esc(row.value)}</td></tr>`).join('')||`<tr><td colspan="2">${state.error?'*****':state.rows?'No matching values.':'Loading SQL values…'}</td></tr>`);
 setHTML(node.querySelector('[data-sql-information]'),state.error?`<div class="notice">SQL values unavailable: ${esc(state.error)}</div>`:'');
 setText(node.querySelector('[data-sql-page-label]'),`${rows.length?start+1:0}–${Math.min(start+state.size,rows.length)} of ${rows.length}${query&&state.rows?' matching / '+state.rows.length+' total':''} · Page ${state.page} of ${pages}`);
 setProperty(node.querySelector('[data-sql-page="previous"]'),'disabled',state.page===1);
 setProperty(node.querySelector('[data-sql-page="next"]'),'disabled',state.page===pages);
}
function prepareSqlTables(root,s){
 sqlViews.set(root,{dbId:s.db_system.id,available:hasTelemetry(s)});
 for(const kind of ['variables','status']){
  const state=sqlState(root,kind);
  if(!hasTelemetry(s)){
   state.generation++;state.pending=null;state.rows=null;state.error=s.telemetry_error||'Database connection unavailable';
  }
  renderSqlTable(root,kind);
 }
}
async function loadSqlTab(root,kind){
 if(!['variables','status'].includes(kind))return;
 const view=sqlViews.get(root);if(!view||!view.available||!root.isConnected||root.closest('.tab-panel')?.hidden)return;
 const state=sqlState(root,kind);if(state.pending)return state.pending;
 const generation=++state.generation,compartment=inventory.compartment_id;
 const pending=(async()=>{
  try{
   const data=await api(`/api/db/${encodeURIComponent(view.dbId)}/sql/${kind}`);
   if(state.generation!==generation||!root.isConnected||inventory.compartment_id!==compartment)return;
   state.rows=data.rows;state.error='';
  }catch(error){
   if(state.generation!==generation||!root.isConnected||inventory.compartment_id!==compartment)return;
   state.rows=null;state.error=error.message;
  }finally{
   if(state.generation===generation){state.pending=null;if(root.isConnected)renderSqlTable(root,kind);}
  }
 })();state.pending=pending;return pending;
}
async function refreshSqlTab(root){if(root)await loadSqlTab(root,systemTabs.get(root.dataset.tabKey));}
function configurationName(s){const id=s.db_system.configuration_id;return id?`${s.configuration?.display_name||'Name unavailable'} [${id}]`:'Not configured';}
const labelNames={id:'OCID',compartment_id:'Compartment OCID',nsg_ids:'Network security groups',subnet_id:'Subnet OCID',
 ip_address:'IP address',ipv6_address:'IPv6 address',is_highly_available:'High availability',configuration_id:'Configuration',
 data_storage_size_in_gbs:'Storage size (GB)',port_x:'MySQL X Protocol port',port:'MySQL port',is_heat_wave_cluster_attached:'HeatWave cluster attached',
 database_mode:'Database mode',access_mode:'Access mode',shape_name:'Shape',mysql_version:'MySQL version',hostname_label:'Hostname label'};
const configLabel=key=>labelNames[key]||key.replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
function configValue(value,key='') {
 if(/password|token|private_key|secret_content|source_url|par_url|preauthenticated/i.test(key))return '<span class="muted">Managed securely</span>';
 if(value===null||value===undefined||value==='')return '<span class="muted">Not configured</span>';
 if(typeof value==='boolean')return value?'Enabled':'Disabled';
 if(Array.isArray(value))return value.length?`<ul class="configuration-array">${value.map(v=>`<li>${configValue(v,key)}</li>`).join('')}</ul>`:'<span class="muted">None</span>';
 if(typeof value==='object')return Object.keys(value).length?`<dl class="configuration-values">${Object.entries(value).sort(([a],[b])=>a.localeCompare(b)).map(([k,v])=>`<div><dt>${esc(configLabel(k))}</dt><dd>${configValue(v,k)}</dd></div>`).join('')}</dl>`:'<span class="muted">Not configured</span>';
 if(/^time_/.test(key)&&!Number.isNaN(Date.parse(value)))return esc(new Date(value).toISOString().replace('T',' ').replace('.000Z',' UTC'));
 return `<span class="configuration-value">${esc(value)}</span>`;
}
function configurationSections(db) {
 const groups=[['Identity & state',['id','display_name','description','compartment_id','lifecycle_state','lifecycle_details','time_created','time_updated']],
 ['Database & availability',['mysql_version','shape_name','is_highly_available','configuration_id','database_mode','access_mode','crash_recovery','availability_domain','fault_domain','current_placement']],
 ['Connections & networking',['hostname_label','ip_address','port','port_x','subnet_id','nsg_ids','endpoints','secure_connections','is_ipv6_enabled','ipv6_address_ipv6_subnet_cidr_pair_details','read_endpoint']],
 ['Storage & encryption',['data_storage_size_in_gbs','data_storage','encrypt_data']],
 ['Backup & recovery',['backup_policy','deletion_policy','point_in_time_recovery_details']],
 ['Maintenance & monitoring',['maintenance','controlled_update','database_management','telemetry_configuration','customer_contacts']],
 ['HeatWave & REST services',['is_heat_wave_cluster_attached','heat_wave_cluster','rest','database_console']],
 ['Source & tags',['source','freeform_tags','defined_tags','system_tags','security_attributes']]];
 const used=new Set(groups.flatMap(([,keys])=>keys));used.add('channels');
 const other=Object.keys(db).filter(k=>!used.has(k));if(other.length)groups.push(['Other settings',other]);
 return groups.filter(([,keys])=>keys.some(k=>k in db)).map(([title,keys])=>`<section class="configuration-group"><h3>${esc(title)}</h3><dl class="configuration-values">${keys.filter(k=>k in db).map(k=>`<div><dt>${esc(configLabel(k))}</dt><dd>${configValue(db[k],k)}</dd></div>`).join('')}</dl></section>`).join('');
}

let currentNotification={text:'No operation in progress.',color:'green',title:'Ready'},failedNotification=null,workflowNotificationKey=null;
const knownFailures=new Set();
function paintNotification(reveal=false) {
 if(reveal)setProperty('#message','hidden',false);
 setProperty('#message','className',`notification-banner ${currentNotification.color}`);
 setText('#message-title',currentNotification.title);setText('#message-text',currentNotification.text);
 setProperty('#message-history','hidden',!currentNotification.jobId);
 const color=failedNotification?'red':currentNotification.color;
 setProperty('#notifications','className',`notification-button ${color}`);
 setProperty('#notifications','title',failedNotification?'Notifications · failure requires attention':`Notifications · ${currentNotification.title}`);
 setProperty('#notification-dot','hidden',!failedNotification);
}
function message(text,color='green',options={}) {
 currentNotification={text,color,title:color==='red'?'Operation requires attention':color==='orange'?'Operation in progress':'Operation completed',...options};
 if(color==='red')failedNotification=currentNotification;
 paintNotification(true);
}
function jobNotification(job) {
 const events=job.events||[],failed=['FAILED','INTERRUPTED'].includes(job.status);
 const active=['QUEUED','ACCEPTED','RUNNING','IN_PROGRESS'].includes(job.status);
 const event=failed?events.find(e=>['FAILED','INTERRUPTED'].includes(e.status)):events.findLast(e=>e.status==='RUNNING')||events.at(-1);
 const operation=job.kind.replaceAll('_',' '),detail=event?.detail;
 return {jobId:job.id,color:failed?'red':active?'orange':'green',
  title:`${operation} · ${job.status==='RUNNING'?'running':job.status.toLowerCase()}`,
  text:failed?`${event?.step||'Operation requires attention'}${detail?'\n'+detail:''}`:
   active?`${event?.step||'Queued; waiting for execution'}${event?.work_request?'\nWork request: '+event.work_request:''}`:
   'All workflow steps completed. View activity for timings and work-request details.'};
}
function syncWorkflowNotification(jobs) {
 const ordered=[...jobs].sort((a,b)=>b.started-a.started);
 for(const job of [...ordered].reverse())if(['FAILED','INTERRUPTED'].includes(job.status)&&!knownFailures.has(job.id)){
  knownFailures.add(job.id);failedNotification=jobNotification(job);
 }
 const job=ordered.find(j=>['RUNNING','IN_PROGRESS'].includes(j.status))||ordered.find(j=>['QUEUED','ACCEPTED'].includes(j.status))||ordered[0];
 if(!job){paintNotification();return;}
 const next=jobNotification(job),key=JSON.stringify(next);
 if(key!==workflowNotificationKey){workflowNotificationKey=key;message(next.text,next.color,next);}
 else paintNotification();
}
function modal(title, content) { $('#modal-title').textContent=title; $('#modal-body').innerHTML=content; $('#modal-error').hidden=true; if (!$('#modal').open) $('#modal').showModal(); }
function modalError(e) { $('#modal-error').textContent=e.message; $('#modal-error').hidden=false; message(e.message,'red'); }
function submitForm(handler) { $('#modal-body form').addEventListener('submit', async e => {e.preventDefault(); const button=e.submitter; button.disabled=true; try {await handler(new FormData(e.target),e.target);} catch(err){modalError(err);} finally {button.disabled=false;} }); }
function conversionGuidance(plan){
 const g=plan.conversion_guidance;if(!g)return '';
 return `<aside class="information" id="conversion-guidance"><h3>Before making Green read-write</h3><p>${esc(g.message)}</p><div class="table-wrap"><table><thead><tr><th>replica_type_conversions</th><th>When to consider it</th></tr></thead><tbody>${g.options.map(([name,description])=>`<tr><th scope="row">${esc(name)}</th><td>${esc(description)}</td></tr>`).join('')}</tbody></table></div><p class="small">${esc(g.note)}</p><p class="small"><a href="${esc(g.source)}" target="_blank" rel="noopener">MySQL type conversion rules</a></p><button type="button" id="review-conversion-configuration">Review Green configuration first</button><p class="small muted">You can also confirm read-write below and keep the current configuration.</p></aside>`;
}
async function queued(path,params,onSuccess) {
 const plan=await api('/api/operation-plan',{path,params});
 const previousTitle=$('#modal-title').textContent,previousNodes=[...$('#modal-body').childNodes],wasOpen=$('#modal').open;
 modal('Confirm · '+plan.title,`<p class="small muted resource-id">Compartment: ${esc(plan.compartment_id)}</p>${conversionGuidance(plan)}<ol class="operation-changes">${plan.changes.map(c=>`<li><span class="small muted">${esc(c.service)}</span><strong>${esc(c.action)}</strong><span>${esc(c.resource)}</span>${c.detail?`<p class="small muted">${esc(c.detail)}</p>`:''}</li>`).join('')||'<li>No resource changes are planned.</li>'}</ol><div class="operation-checks"><h3>Execution checks</h3><ul>${plan.checks.map(c=>`<li>${esc(c)}</li>`).join('')}</ul></div><p class="small muted">Current state is checked again during execution. Changes run only after confirmation.</p><div class="form-footer"><button type="button" id="cancel-operation">Cancel</button><button type="button" id="confirm-operation" class="primary" ${!plan.execution_allowed?'disabled':''}>Confirm and execute</button></div>${!plan.execution_allowed?'<p class="small muted">Execution is disabled in this operating mode.</p>':''}`);
 if(plan.conversion_guidance)$('#review-conversion-configuration').addEventListener('click',()=>configurationDialog(plan.conversion_guidance.db_system_id,'replica_type_conversions').catch(modalError));
 $('#cancel-operation').addEventListener('click',()=>{if(wasOpen){$('#modal-title').textContent=previousTitle;$('#modal-body').replaceChildren(...previousNodes);$('#modal-error').hidden=true;}else $('#modal').close();});
 $('#confirm-operation').addEventListener('click',async e=>{
  e.currentTarget.disabled=true;
  try {
   message('Submitting '+plan.title+'…','orange',{title:'Submitting operation'});
   const result=await api(path,plan.confirmation_token?{...params,confirmation_token:plan.confirmation_token,confirmed:true}:params);$('#modal').close();
   message(result.job_id?'Operation queued. Live progress appears here; View activity opens its execution details.':'Operation completed.',result.job_id?'orange':'green',{jobId:result.job_id});
   await refresh();if(onSuccess)await onSuccess(result);
  }catch(error){modalError(error);if($('#confirm-operation'))$('#confirm-operation').disabled=false;}
 });
}
const footer = text => `<div class="form-footer"><button type="submit" class="primary" ${mutationsDisabled ? 'disabled' : ''}>${text}</button></div>`;

function renderJobs(jobs) {
 return jobs.map(j=>`<article class="job" data-resource-id="${esc(j.id)}"><div class="job-head"><div><strong>${esc(j.kind.replaceAll('_',' '))}</strong><p class="small muted">${time(j.started)} · ${duration(j.started,j.ended)} · ${esc(j.resource)}</p></div>${badge(j.status)}</div><div class="timeline">${j.events.map(e=>`<div class="timeline-row"><i class="dot ${tone(e.status)}"></i><div class="timeline-content"><div class="timeline-step">${esc(e.step)}</div>${e.work_request ? `<div class="timeline-actions"><button data-work="${esc(e.work_request)}">Work request details</button></div>`:''}${e.detail ? `<details><summary>Details</summary><pre>${esc(e.detail)}</pre></details>`:''}</div><span class="muted">${duration(e.started,e.ended)}</span></div>`).join('')}</div><details><summary class="small">Registered choices and secret references</summary><pre class="gtid">${pretty(j.params)}</pre></details></article>`).join('') || '<p class="muted">No activity recorded yet.</p>';
}
function showWorkRequest(data) {
 const work=data.request,warnings=data.warnings||[],comp=work.compartment_id||inventory.compartment_id;
 const unavailable=section=>data[section+'_available']===false;
 const section=`<div class="form-section"><h3>Errors</h3>${unavailable('errors')?'<p class="small muted">Unavailable · could not be verified. See the diagnostic notice above.</p>':data.errors?.length?`<pre class="gtid">${pretty(data.errors)}</pre>`:'<p class="small muted">No reported errors.</p>'}</div>`;
 modal('Work request details',`<div class="panel-title"><h3>${esc(work.operation_type||'OCI work request')}</h3>${badge(work.status)}</div><p class="small muted">The status above is reported by GetWorkRequest. Diagnostic availability does not change that status.</p>${warnings.map(w=>`<div class="information"><strong>${esc(w.operation)} · ${esc(w.section)} unavailable</strong><p>${esc(w.error)}</p><p class="small">${esc(w.information)}</p></div>`).join('')}${warnings.length?`<details class="information"><summary>Compartment-scoped access guidance</summary><p>Status access succeeded. A diagnostic 404 can mean unavailable resources or missing authorization; it does not establish which. If your IAM administrator confirms a missing work-request read grant, verify this policy for this compartment only:</p><pre class="gtid">${esc('Allow dynamic-group <vm-dynamic-group> to read mysql-work-requests in compartment id '+comp)}</pre><p class="small">This may already be covered by a MySQL read/manage policy. If it is, provide the diagnostic operation and request ID to Oracle Support; no tenancy-wide grant is suggested.</p></details>`:''}${configValue(work)}${section}`);
 if(warnings.length||work.status==='FAILED')message(`OCI work-request status: ${work.status}. ${warnings.map(w=>w.operation+' unavailable: '+w.error).join(' ')}`,work.status==='FAILED'?'red':'orange',{title:work.status==='FAILED'?'OCI work request failed':'Work-request diagnostic information'});
}
function channelMatches(c) {
 const state=$('#channel-filter').value;
 return !state || (state==='ENABLED' ? c.is_enabled : state==='DISABLED' ? !c.is_enabled : c.lifecycle_state===state);
}
function renderFleet(autoSelect=true) {
 if(!inventory)return;
 const query=$('#search').value.toLowerCase(), state=$('#state-filter').value, replicas=$('#replica-filter').value;
 const systems=inventory.systems.filter(s=>s.lifecycle_state!=='DELETED' && Boolean(s.is_highly_available)===(fleet==='ha') &&
  `${s.display_name} ${s.id} ${s.ip_address||''}`.toLowerCase().includes(query) && (!state || s.lifecycle_state===state) &&
  (!$('#channel-filter').value || s.channels.some(channelMatches)) &&
  (!replicas || (replicas==='present' ? s.read_replicas.length>0 : s.read_replicas.length===0)))
  .sort((a,b)=>(a.display_name||'').localeCompare(b.display_name||'')*(descending?-1:1));
 setText('#inventory-count',`${systems.length} DB system${systems.length===1?'':'s'} shown · deleted resources hidden`);
 setHTML('#systems',systems.map(s=>`<button class="system-choice ${s.blue_green_role?.toLowerCase()||''} ${inspected===s.id?'active':''}" data-inspect="${esc(s.id)}" aria-pressed="${inspected===s.id}"><div><strong class="server-name ${s.blue_green_role?.toLowerCase()||''}">${esc(s.display_name)}</strong>${badge(s.lifecycle_state,s.tone)}</div>${s.blue_green_role?`<span class="role-chip ${s.blue_green_role.toLowerCase()}">${esc(s.blue_green_role)}${s.is_current_primary?' · CURRENT PRIMARY':''}</span>`:''}<span class="small muted">${esc(s.ip_address||'Endpoint pending')} · ${esc(s.mysql_version)}</span><span class="small muted">${s.channels.length} channel${s.channels.length===1?'':'s'} · ${s.read_replicas.length} read replica${s.read_replicas.length===1?'':'s'}</span>${s.read_replicas.length?'<span class="small orange">Blue-green unavailable: read replicas present</span>':''}</button>`).join('') || '<p class="muted">No DB systems match these filters.</p>');
 if(autoSelect && !systems.some(s=>s.id===inspected)) {
  if(systems.length)showSystem(systems[0].id);
  else {inspected=null;systemDetailVersion++;setHTML('#system-detail','<p class="muted">No DB system matches the current filters.</p>');}
 }
}
function replicaRows(replicas) {
 return replicas.map(r=>`<article class="resource-detail"><div class="resource-heading"><strong>${esc(r.display_name)}</strong>${badge(r.lifecycle_state)}</div><dl class="facts"><div><dt>Endpoint</dt><dd>${esc(r.ip_address||'Pending')}:${esc(r.port||3306)}</dd></div><div><dt>Version / shape</dt><dd>${esc(r.mysql_version||'')} / ${esc(r.shape_name||'')}</dd></div></dl><details><summary>Read replica configuration</summary><pre class="gtid">${pretty(r)}</pre></details></article>`).join('') || '<p class="small muted">No read replicas.</p>';
}
const facts = values => `<dl class="facts">${values.map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v??'Unavailable')}</dd></div>`).join('')}</dl>`;

const hasTelemetry=s=>!s.telemetry_error && s.telemetry_available!==false;
const liveValue=(s,value)=>hasTelemetry(s)?value:'*****';
const observedFacts=(s,values)=>facts(values.map(([label,value])=>[label,liveValue(s,value)]));
const telemetryNotice=s=>hasTelemetry(s)?'':`<div class="information">Database observation unavailable. Status values show ***** until a successful check.<p class="small">${esc(s.telemetry_error||'Database observation failed')}</p></div>`;
function clearLiveReadout(root,error) {
 if(root)for(const view of root.querySelectorAll('[data-tabview]')){const info=sqlViews.get(view);if(info)info.available=false;for(const kind of ['variables','status']){const state=sqlState(view,kind);state.generation++;state.pending=null;state.rows=null;state.error=error;renderSqlTable(view,kind);}}
 if(!root)return;
 const areas=[root,...root.querySelectorAll('*')];areas.forEach(node=>renderedHTML.delete(node));
 root.querySelectorAll('.facts dd,.gtid,tbody td').forEach(node=>setText(node,'*****'));
 let notice=root.querySelector('[data-observation-error]');
 if(!notice){notice=document.createElement('div');notice.className='information';notice.style.gridColumn='1 / -1';notice.dataset.observationError='true';root.prepend(notice);}
 setText(notice,'Database observation unavailable. Status values show ***** until a successful check. '+error);
}
function resetFailedReadout(root){root?.querySelectorAll('[data-observation-error]').forEach(node=>node.remove());}

let inspected=null,systemDetailVersion=0;
const systemRequests=new Map();
function operationBusy(){return inventory?.history.some(j=>['QUEUED','ACCEPTED','RUNNING','IN_PROGRESS'].includes(j.status));}
function renderCreationAvailability(activeJobs=[],error=''){
 const reasons=[];
 if(mutationsDisabled)reasons.push(demo?'Creation is disabled in demo mode.':'Creation is disabled in read-only validation mode.');
 let blockedSources=[];
 if(error)reasons.push(error);
 else if(inventory){
  if(activeJobs.length)reasons.push(`Wait for the current operation to finish: ${activeJobs.map(j=>`${j.kind.replaceAll('_',' ')} (${j.status.toLowerCase()})`).join(', ')}.`);
  if(!inventory.systems.some(s=>s.eligible)){
   reasons.push(inventory.systems.length?'No eligible source DB system in the selected compartment.':inventory.db_scope_verified===false?'DB-system discovery could not be verified. Review access for the selected compartment.':'No DB systems found in the selected compartment.');
   blockedSources=inventory.systems;
  }
 }
 const blocked=reasons.length>0;
 setProperty('#new-deployment','disabled',blocked);
 setProperty('#new-deployment','title',blocked?reasons.join(' '):'Create an environment from an eligible source DB system');
 setProperty('#creation-availability','hidden',!blocked);
 setHTML('#creation-availability',blocked?`<strong>Creation unavailable</strong>${reasons.map(reason=>`<p>${esc(reason)}</p>`).join('')}${blockedSources.length?`<details data-detail-key="creation-sources"><summary>Source DB system eligibility</summary><ul>${blockedSources.slice(0,10).map(s=>`<li><strong>${esc(s.display_name)}</strong> · ${esc(s.lifecycle_state)}: ${(s.creation_blockers||['Eligibility could not be verified']).map(esc).join('; ')}</li>`).join('')}</ul><p>${blockedSources.length>10?`${blockedSources.length-10} additional DB systems. `:''}Review individual requirements in DB Systems.</p></details>`:''}`:'');
}
function lifecycleControls(s){
 const d=s.db_system,actions=s.lifecycle_actions||{},protection=d.deletion_policy?.is_delete_protected;
 const labels=[['start','Start'],['stop','Stop'],...(protection===true?[['disable_delete_protection','Disable deletion protection']]:[]),['delete','Delete DB system']];
 return `<div class="lifecycle-controls"><p class="small muted">Deletion protection: ${protection===true?'Enabled':protection===false?'Disabled':'Unverified'}</p><div class="actions"><button data-configure="${esc(d.id)}" ${mutationsDisabled||operationBusy()?'disabled':''}>Edit configuration</button>${labels.map(([action,label])=>`<button data-db="${esc(d.id)}" data-action="${action}" title="${esc(actions[action]?.reason||label)}" ${action==='delete'?'class="danger-action"':''} ${mutationsDisabled||operationBusy()||!actions[action]?.allowed?'disabled':''}>${label}</button>`).join('')}</div></div>`;
}
function disableDeploymentActions(reason){for(const id of ['switchover','delete-deployment','precheck']){setProperty('#'+id,'disabled',true);setProperty('#'+id,'title',reason);}}
function systemShell() {
 return '<div id="db-pair"></div><div id="db-summary"><p class="muted">Loading DB system details…</p></div>'+detailTabMarkup('db','DB system details',systemKey(),true);
}
async function showSystem(id) {
 const previous=uiRevision,changedSelection=inspected!==id||!$('#db-summary');
 if(changedSelection){inspected=id;systemDetailVersion++;renderedHTML.delete($('#system-detail'));setHTML('#system-detail',systemShell());renderFleet(false);activateSystemTab(systemTabs.get(systemKey(id))||'details');}
 const version=systemDetailVersion,compartment=inventory.compartment_id,key=systemKey(id);
 try {
  if(!systemRequests.has(key))systemRequests.set(key,api('/api/db/'+encodeURIComponent(id)).finally(()=>systemRequests.delete(key)));
  const s=await systemRequests.get(key);
  if(version!==systemDetailVersion||inspected!==id||inventory.compartment_id!==compartment)return;
  resetFailedReadout($('#system-detail'));
  const d=s.db_system,item=inventory.systems.find(x=>x.id===id),channels=(s.channels||[]).map(c=>({...c,health:c.health||inventory.channels.find(x=>x.id===c.id)?.health}));
  const context=s.blue_green_context;
  setHTML('#db-pair',`${context?`<div class="paired-environment"><p class="eyebrow">${esc(context.display_name||'BLUE GREEN ENVIRONMENT')}</p><div class="paired-servers">${context.members.map(member=>`<button class="paired-server ${member.role.toLowerCase()} ${member.selected?'selected':''}" data-related-db="${esc(member.id)}" aria-pressed="${Boolean(member.selected)}" title="View ${esc(member.role)} server" ${!member.available?'disabled':''}><span class="small">${esc(member.role)}${member.is_current_primary?' · CURRENT PRIMARY':''}</span><strong>${esc(member.display_name)}</strong><span class="small">${member.selected?'Selected · ':''}${esc(member.lifecycle_state||'Details unavailable')}${member.information?' · '+esc(member.information):''}</span></button>`).join('')}</div></div>`:''}${s.relationship_warning?`<div class="information">${esc(s.relationship_warning)}</div>`:''}`);
  setHTML('#db-summary',`<div class="panel-title"><div><p class="eyebrow">${item?.blue_green_role?esc(item.blue_green_role)+' SERVER'+(item.is_current_primary?' · CURRENT PRIMARY':''):'DB SYSTEM DETAILS'}</p><h2 class="server-name ${item?.blue_green_role?.toLowerCase()||''}">${esc(d.display_name)}</h2></div>${badge(d.lifecycle_state,item?.tone)}</div><p class="small muted resource-id">${esc(d.id)}</p><div class="actions"><button data-observe="${esc(id)}">SQL access</button><button data-events="${esc(id)}">Events</button><button data-create="${esc(id)}" class="primary" ${!item?.eligible||mutationsDisabled?'disabled':''}>Create blue-green</button><button data-db="${esc(id)}" data-action="${d.is_highly_available?'ha_off':'ha_on'}" ${mutationsDisabled||d.lifecycle_state!=='ACTIVE'?'disabled':''}>${d.is_highly_available?'Disable HA':'Enable HA'}</button></div>${lifecycleControls(s)}${item?.creation_blockers?.length?`<div class="notice creation-blockers">${item.creation_blockers.map(esc).join('<br>')}</div>`:''}`);
  renderServerDetails('db',s,channels,'db:'+key,true);
  await refreshSqlTab($('#db-tabview'));
  stampChanges(previous);
 }catch(e){if(version===systemDetailVersion){clearLiveReadout($('#system-detail'),e.message);setHTML('#db-summary',`<div class="alert red">${esc(e.message)}</div>`);}}
}
function renderServerDetails(prefix,s,channels,requestKey,filterChannels=false) {
 const d=s.db_system;
 const endpoints=d.endpoints?.length?d.endpoints:[{ip_address:d.ip_address,hostname:d.hostname_label,port:d.port,modes:[d.database_mode]}];
  setHTML(`#${prefix}-connection`,`<h3>Connection & configuration</h3><p class="small muted">Version, database/access mode, hostname and uptime: live SQL. HA, shape, configuration and network endpoints: OCI.</p>${s.configuration_information?`<div class="information">${esc(s.configuration_information)}</div>`:''}${telemetryNotice(s)}${observedFacts(s,[['Deployment',d.is_highly_available?'High availability':'Standalone'],['Database mode',s.database_mode??'*****'],['MySQL version',s.mysql_version??'*****'],['Version comment',s.version_comment??'*****'],['Access mode',s.access_mode??'*****'],['Shape',d.shape_name],['HA / DB configuration',configurationName(s)],['SQL hostname',s.hostname||'SQL observation required'],['Placement',`${d.availability_domain||''} ${d.fault_domain||''}`]])}<dl class="facts"><div><dt>Uptime</dt><dd id="${prefix}-uptime"></dd></div></dl><div class="table-wrap"><table><thead><tr><th>Endpoint / hostname</th><th>Address</th><th>Ports</th><th>Modes / state</th></tr></thead><tbody>${endpoints.map(e=>`<tr><td>${esc(liveValue(s,e.hostname||d.hostname_label||'Not assigned'))}</td><td>${esc(liveValue(s,e.ip_address||d.ip_address||'Pending'))}${e.ipv6_address?'<br>'+esc(liveValue(s,e.ipv6_address)):''}</td><td>MySQL ${esc(liveValue(s,e.port||d.port||3306))}<br>X ${esc(liveValue(s,e.port_x||d.port_x||33060))}</td><td>${esc(liveValue(s,(e.modes||[]).join(', ')))}<br>${esc(liveValue(s,e.status||d.lifecycle_state))}</td></tr>`).join('')}</tbody></table></div>`);
  setText(`#${prefix}-uptime`,liveValue(s,s.uptime===undefined?'Unavailable':s.uptime+' seconds'));
  setHTML(`#${prefix}-replicas`,`<h3>Read replicas (${s.read_replicas.length})</h3>${s.replica_error?`<div class="notice">Discovery unverified: ${esc(s.replica_error)}</div>`:''}${replicaRows(s.read_replicas)}`);
  setHTML(`#${prefix}-channels`,`<h3>Replication channels (${channels.length})</h3>${filterChannels && $('#channel-filter').value?`<p class="small muted">Filtered by ${esc($('#channel-filter').value)}.</p>`:''}${channels.filter(c=>!filterChannels||channelMatches(c)).map(c=>`<article class="resource-detail" data-resource-id="${esc(c.id)}">${channelRows([c])}<details data-detail-key="${esc(c.id)}"><summary>Source, target, filters & health</summary><pre class="gtid">${pretty(c)}</pre></details></article>`).join('')||'<p class="small muted">No matching replication channels.</p>'}`);
  setHTML(`#${prefix}-gtids`,`<h3 class="detail-section-title">GTIDs & SQL replication</h3>${s.telemetry_error?`<div class="notice">SQL telemetry unavailable: ${esc(s.telemetry_error)}</div>`:''}${observedFacts(s,[['Server UUID',s.server_uuid],['Read only',s.read_only===undefined?'Unavailable':String(s.read_only)],['Super read only',s.super_read_only===undefined?'Unavailable':String(s.super_read_only)]])}<p class="eyebrow">EXECUTED GTID SET</p><pre class="gtid">${esc(liveValue(s,s.gtid||'Configure SQL access with a Vault password secret to observe GTIDs.'))}</pre><details><summary>SQL replication activities and GTID details</summary><pre class="gtid">${hasTelemetry(s)?pretty(s.replication||[]):'*****'}</pre></details>`);
  setHTML(`#${prefix}-work-scope`,`<p class="small muted">${esc(s.work_request_scope||'Work requests for this DB system')}</p>${s.work_request_warning?`<div class="information">${esc(s.work_request_warning)}</div>`:''}`);
  renderWorkRequests(`#${prefix}-work-requests`,s.work_requests,requestKey);
  setHTML(`#${prefix}-configuration`,'<p class="small muted">OCI control-plane settings. Current SQL values are shown in Details, Variables and Status.</p>'+configurationSections({...d,configuration_id:configurationName(s)}));
  prepareSqlTables($('#'+prefix+'-tabview'),s);
}
function channelRows(channels) {
 return channels.filter(c=>c.lifecycle_state!=='DELETED').map(c=>{
  const health=c.health||{},result=health.channel_status_result||{},healthy=result.is_healthy;
  const information=[c.target?.channel_name,health.time_updated?`OCI snapshot: ${health.time_updated} (expires after 12 hours)`:'',health.information||'',health.error?`Health unavailable: ${health.error}`:'',healthy===false?'Fix or delete this channel':''];
  return `<div class="channel">${badge(c.is_enabled ? (healthy===true ? 'ACTIVE · HEALTHY' : healthy===false ? 'UNHEALTHY' : 'HEALTH UNKNOWN') : 'DISABLED', healthy===false ? 'red' : c.is_enabled && healthy!==true ? 'orange':'green')} ${esc(c.display_name)}<span class="small muted">${information.filter(Boolean).map(esc).join(' · ')}</span><div class="row-actions"><button data-channel="${esc(c.id)}" data-action="refresh_channel_status" title="Generate a fresh OCI replication-health snapshot" ${mutationsDisabled||operationBusy()||!['ACTIVE','INACTIVE'].includes(c.lifecycle_state)?'disabled':''}>Refresh snapshot</button><button data-channel="${esc(c.id)}" data-action="${c.is_enabled ? 'disable_channel':'enable_channel'}" ${mutationsDisabled?'disabled':''}>${c.is_enabled ? 'Disable':'Enable'}</button><button data-channel="${esc(c.id)}" data-action="delete_channel" ${mutationsDisabled?'disabled':''}>Delete</button></div></div>`;
 }).join('') || '<span class="muted">None</span>';
}
let discoveryMessage=null;
async function refresh() {
 const previous=uiRevision;
 if(refreshing)return; refreshing=true;
 try {
  inventory=await api('/api/dashboard');
  const activeJobs=inventory.history.filter(j=>['QUEUED','ACCEPTED','RUNNING','IN_PROGRESS'].includes(j.status));
  renderCreationAvailability(activeJobs);
  const warnings=[inventory.access_warning||inventory.registration_warning,inventory.replica_warning && !inventory.access_warning?inventory.replica_warning:null].filter(Boolean);
  if(warnings.length && currentNotification.color!=='red' && !inventory.history.some(j=>j.status==='RUNNING'))message(warnings.join(' ')+' New creation is disabled until discovery is verified.','orange',{title:'Compartment discovery warning',category:'discovery'});
  else if(!warnings.length && discoveryMessage && currentNotification.category==='discovery')message('No operation in progress.','green',{title:'Ready',category:'idle'});
  discoveryMessage=warnings.join(' ');
  setProperty('#discovery-diagnostics','hidden',false);
  const policy=`Allow dynamic-group heatwave-bluegreen-vm to read mysql-family in compartment id ${inventory.compartment_id}`;
  const executionPolicy=`Allow dynamic-group heatwave-bluegreen-vm to manage mysql-family in compartment id ${inventory.compartment_id}`;
  setHTML('#discovery-details',`<p class="small">${esc(inventory.discovery_scope?.information||'Discovery is confined to the selected compartment. No tenancy-wide MySQL access is required.')}</p><p class="small">Out-of-compartment discovery is disabled. Lack of access to other compartments is informational and does not block creation here. No permissions are changed by this console.</p>`+
   (inventory.registration_diagnostics||[]).map(d=>`<p><strong>${esc(d.operation)}</strong> · ${esc(d.compartment_name)}</p><p class="small resource-id">${esc(d.compartment_id)}</p><p class="small resource-id">${esc(d.error)}</p>`).join('')+
   `<p class="small"><strong>Suggested IAM policy for this compartment only</strong><br>Replace the example dynamic-group name with the group that matches this VM. Attach the policy in this compartment or an ancestor that can grant access to it.</p><pre class="gtid">${esc(policy)}</pre><p class="small">Only if live creation, HA, channel or switchover operations are intended:</p><pre class="gtid">${esc(executionPolicy)}</pre><p class="small">Vault secret reads and creation need separate permissions in the explicitly selected secret compartment. If a Vault, key, subnet or deployment is outside this compartment, request access for that named compartment separately. No tenancy-wide grant is assumed. OCI checks conflicts with deployments outside this view when a creation request is submitted.</p>`);
  setText('#region',inventory.region);setText('#compartment-id',inventory.compartment_id);
  setText('#compartment-name',inventory.compartment_id===inventory.default_compartment_id?'VM compartment':'Selected compartment');
  const counts=[inventory.deployments.filter(d=>d.lifecycle_state!=='DELETED').length,inventory.systems.length,inventory.work_requests.filter(w=>['ACCEPTED','IN_PROGRESS'].includes(w.status)).length,inventory.systems.filter(s=>s.tone==='red').length];
  setHTML('#metrics',['Blue-green environments','DB systems','Active work requests','Require attention'].map((s,i)=>`<div class="metric"><span class="small muted">${s}</span><strong class="${i===3&&counts[i]?'red':i===2&&counts[i]?'orange':''}">${counts[i]}</strong><span class="small muted">${['Provisioned in this compartment','HA and standalone','Tracked on this dashboard','DB or replication errors'][i]}</span></div>`).join(''));
  setHTML('#deployments',inventory.deployments.filter(d=>d.lifecycle_state!=='DELETED').map(d=>`<button class="deployment ${selected===d.id?'active':''}" data-select="${esc(d.id)}"><div><strong>${esc(d.display_name)}</strong><span class="small muted">${esc(d.switchover_status)} · ${esc(d.lifecycle_details || '')}</span></div><div class="deployment-flow"><span class="blue-chip">Blue</span>→<span class="green-chip">Green</span></div>${badge(d.lifecycle_state,d.tone)}</button>`).join('') || '<p class="muted">No blue-green environments. Choose a DB system to begin.</p>');
  setText('#standalone-count',inventory.systems.filter(s=>!s.is_highly_available).length);
  setText('#ha-count',inventory.systems.filter(s=>s.is_highly_available).length);
  const previousSystem=inspected;renderFleet();
  if(inspected && previousSystem===inspected)await showSystem(inspected);
  const latest=[...inventory.history].sort((a,b)=>b.started-a.started).slice(0,1);
  setHTML('#active-jobs',renderJobs(activeJobs.length?activeJobs:latest));
  setHTML('#history-list',renderJobs(inventory.history));
  syncWorkflowNotification(inventory.history);

  if(selected && inventory.registration_scope_verified!==false && !inventory.deployments.some(d=>d.id===selected && d.lifecycle_state!=='DELETED')){selected=null;detailData=null;setProperty('#selected','hidden',true);disableDeploymentActions('Deployment is no longer available.');}
  if(selected)await showDetail(selected);
  stampChanges(previous);
 } catch(e){renderCreationAvailability([],'Current inventory could not be verified. Refresh the dashboard before creating an environment.');disableDeploymentActions('Current deployment state could not be verified.');clearLiveReadout($('#system-detail'),e.message);clearLiveReadout($('#panels'),e.message);message(e.message,'red');} finally{refreshing=false;}
}
let deploymentPanelKey=null;
async function showDetail(id) {
 if(selected!==id)disableDeploymentActions('Loading current deployment state…');
 selected=id;const compartment=inventory.compartment_id;let detail;try{detail=await api(`/api/deployments/${encodeURIComponent(id)}`);}catch(error){if(selected===id && inventory.compartment_id===compartment){disableDeploymentActions('Current deployment state could not be verified.');clearLiveReadout($('#panels'),error.message);}throw error;}resetFailedReadout($('#panels'));
 if(selected!==id||inventory.compartment_id!==compartment)return;detailData=detail;
 setProperty('#selected','hidden',false);setText('#selected-title',detail.deployment.display_name);
 const busy=inventory?.history.some(j=>['QUEUED','ACCEPTED','RUNNING','IN_PROGRESS'].includes(j.status));
 for(const [id,action] of [['precheck','switchover'],['switchover','switchover'],['delete-deployment','delete']]){
  const state=detail.actions?.[action];setProperty('#'+id,'disabled',mutationsDisabled||busy||!state?.allowed);setProperty('#'+id,'title',state?.reason||'');
 }
 setProperty('#activate-copies','disabled',mutationsDisabled||busy);
 setText('#deployment-action-info',detail.actions?.switchover?.reason||'Blue is the current primary. Switchover is available after readiness checks.');
 const key=JSON.stringify([compartment,id,...['blue','green'].map(side=>detail.panels[side]?.db_system.id)]);
 if(deploymentPanelKey!==key){
  deploymentPanelKey=key;
  setHTML('#panels',['blue','green'].map(side=>{
   const panel=detail.panels[side],prefix='deployment-'+side;
   return `<article id="${prefix}-panel" class="panel deployment-server ${side==='green'?'green-panel':''}" aria-label="${side==='blue'?'Blue':'Green'} DB system"><div id="${prefix}-summary"></div>${panel?detailTabMarkup(prefix,`${side==='blue'?'Blue':'Green'} DB system details`,`${compartment}:${id}:${side}:${panel.db_system.id}`):`<p class="muted">${esc(detail.panel_information?.[side]||'DB system is provisioning…')}</p>`}</article>`;
  }).join(''));
 }
 for(const side of ['blue','green']){
  const panel=detail.panels[side];if(!panel)continue;
  const d=panel.db_system,prefix='deployment-'+side,item=inventory.systems.find(x=>x.id===d.id);
  setHTML('#'+prefix+'-summary',`<div class="panel-title"><div><p class="eyebrow"><span class="role-chip ${side}">${side.toUpperCase()}</span> ${detail.deployment.active_db_system_id===d.id?'· CURRENT PRIMARY':''}</p><h3 class="server-name ${side}">${esc(d.display_name)}</h3></div>${badge(d.lifecycle_state,item?.tone)}</div><p class="small muted resource-id">${esc(d.id)}</p><div class="actions"><button data-observe="${esc(d.id)}">SQL access</button><button data-events="${esc(d.id)}">Events</button><button data-db="${esc(d.id)}" data-action="${d.is_highly_available?'ha_off':'ha_on'}" ${mutationsDisabled||d.lifecycle_state!=='ACTIVE'?'disabled':''}>${d.is_highly_available?'Disable HA':'Enable HA'}</button>${side==='green'?`<button data-db="${esc(d.id)}" data-action="${panel.database_mode==='READ_WRITE'?'read_only':'read_write'}" ${mutationsDisabled||!hasTelemetry(panel)||!panel.database_mode||d.lifecycle_state!=='ACTIVE'?'disabled':''}>${panel.database_mode==='READ_WRITE'?'Set read-only':'Allow read-write'}</button>`:''}</div>${lifecycleControls(panel)}`);
  renderServerDetails(prefix,panel,panel.channels||[],`deployment:${compartment}:${id}:${side}`);
  const root=$('#'+prefix+'-tabview');activateDetailTab(root,systemTabs.get(root.dataset.tabKey)||'details',false,false);await refreshSqlTab(root);
 }
 const v=detail.verification;
 setHTML('#verification',v?`<div class="alert ${v.exact?'green':'orange'}">${v.exact?'Absolute same GTID confirmed':'Blue GTIDs verified with additional Green transactions'} ${esc(v.extra_green||'')}</div>`:'');
}
async function catalog(comp=inventory.compartment_id) { return await api('/api/catalog?compartment_id='+encodeURIComponent(comp)); }
const secretOptions = data => [['','Select a Vault secret'],...data.secrets.map(s=>[s.id,s.secret_name])];
async function observerDialog(id) {
 const registration=await api('/api/observer/'+encodeURIComponent(id)),profile=registration.profile||{},secretCompartment=profile.secret_compartment_id||inventory.compartment_id;
 const c=await catalog(secretCompartment), compartments=await api('/api/compartments');
 modal('SQL access · observation & event management',`<p class="muted">The account must see all scheduled events, read telemetry, and have EVENT privileges for managed changes. Passwords stay in OCI Vault.</p>${profile.inherited_from?'<div class="information">Using the paired Blue server’s SQL username and Vault secret automatically. No separate Green registration is required. Save only to configure a separate profile for this server.</div>':''}${registration.information?`<div class="information">${esc(registration.information)}</div>`:''}<form>${field('SQL username','username',profile.username||'', 'text',true)}${select('Password secret *','secret_id',secretOptions(c),profile.secret_id||'')}${select('Secret compartment','secret_compartment_id',compartments.map(c=>[c.id,c.name]),secretCompartment)}<div class="form-footer"><button type="submit" class="primary">Save SQL access</button></div></form>`);
 $('[name=secret_compartment_id]').addEventListener('change',async()=>{try{const c=await catalog($('[name=secret_compartment_id]').value);$('[name=secret_id]').innerHTML=secretOptions(c).map(o=>`<option value="${esc(o[0])}">${esc(o[1])}</option>`).join('');}catch(e){modalError(e);}});
 submitForm(async f=>queued('/api/observer/'+encodeURIComponent(id),Object.fromEntries(f),()=>message('SQL observation profile saved.')));
}
function eventsMarkup(review, mode, originals=[]) {
 const stateMap=new Map(originals.map(e=>[e.event_schema+'\0'+e.event_name,e.status]));
 return `<p class="small muted">${mode==='create'?'Selected enabled source events are disabled before execution and restored to their exact original states after successful creation.':mode==='source'?'Choose source events to disable before promotion. The retired source keeps the selected disabled state.':'Choose the final event states. Defaults follow the recorded original source states. New events keep their current state.'}</p>${mode==='create'||mode==='source'?'<button type="button" data-all-events>Disable all enabled events before execution</button>':''}<div class="table-wrap"><table class="events-table"><thead><tr><th>Event / definition</th><th>Current state</th><th>Schedule</th><th>${mode==='create'||mode==='source'?'Disable first':'Final state'}</th></tr></thead><tbody>${review.events.map((e,i)=>{const original=stateMap.get(e.event_schema+'\0'+e.event_name);const chosen=original==='ENABLED'?'ENABLE':original==='DISABLED'?'DISABLE':'KEEP'; return `<tr><td><strong>${esc(e.event_schema)}.${esc(e.event_name)}</strong><span class="small muted">${esc(e.definer)}</span><details><summary>Definition</summary><pre class="event-definition">${esc(e.definition)}</pre></details></td><td>${badge(e.status,e.status==='ENABLED'?'green':'orange')}<span class="small muted">Original: ${esc(original||e.status)}</span></td><td>${esc(e.interval_value||e.execute_at||'')} ${esc(e.interval_field||'')}<br>${esc(e.time_zone)}</td><td>${mode==='create'||mode==='source'?`<label class="checkbox-field"><input type="checkbox" data-event-index="${i}" ${e.status!=='ENABLED'?'disabled':''}>Disable</label>${mode==='create'?'<span class="small muted">Restore original after creation</span>':''}`:`<select data-event-index="${i}">${[['KEEP','Keep current'],['ENABLE','Enabled'],['DISABLE','Disabled']].map(([v,l])=>`<option value="${v}" ${v===chosen?'selected':''}>${l}</option>`).join('')}</select>`}</td></tr>`;}).join('')||'<tr><td colspan="4">No scheduled events. Inventory must still be reviewed.</td></tr>'}</tbody></table></div>`;
}
function eventChoices(container, review, mode) {
 return {token:review.token, choices:review.events.map((e,i)=>{const control=container.querySelector(`[data-event-index="${i}"]`);return {disable_before:control?.type==='checkbox' && control.checked,after:mode==='create'?'RESTORE':mode==='source'?'KEEP':control?.value||'KEEP'};})};
}
async function manageEvents(id) {
 const review=await api('/api/events/'+encodeURIComponent(id));
 modal('Scheduled events · choose states',`<form><p class="notice">Event changes execute ALTER EVENT and are logged. Disabling an event does not stop an invocation already running.</p><div id="event-edit">${eventsMarkup(review,'target',review.originals)}</div>${footer('Review event changes')}</form>`);
 submitForm(async()=>queued('/api/db/'+encodeURIComponent(id)+'/action',{action:'events',event_review:eventChoices($('#event-edit'),review,'target')}));
}
async function createDialog(id) {
 if(!inventory)return;
 if(!id) {
  const eligible=inventory.systems.filter(s=>s.eligible);
  modal('Create blue-green environment',`<form>${select('Source DB system','source',eligible.map(s=>[s.id,`${s.display_name} · ${s.is_highly_available?'HA':'Standalone'}`]))}<p class="muted">DB systems already registered in blue-green deployments or with read replicas are excluded. Configure SQL access for the source before reviewing events.</p><div class="form-footer"><button class="primary" ${!eligible.length||mutationsDisabled?'disabled':''}>Continue</button></div></form>`);
  submitForm(async f=>createDialog(f.get('source'))); return;
 }
 const db=inventory.systems.find(s=>s.id===id);
 const [c,review,compartments,initial]=await Promise.all([catalog(),api('/api/events/'+encodeURIComponent(id)),api('/api/compartments'),api('/api/db/'+encodeURIComponent(id)+'/creation-options')]);
 const configurationChoices=configs=>configs.map(c=>[c.id,c.display_name+' · '+c.id]);
 const channels=inventory.channels.filter(ch=>ch.target?.db_system_id===id);
 modal('Create environment · '+db.display_name,`<form><div class="form-grid">${field('Environment name','display_name',db.display_name+' · blue green','text',true)}${select('Target MySQL version *','mysql_version',initial.versions.map(v=>[v.version,v.version+' · '+(v.version_family||v.description||'')]),db.mysql_version)}${select('Target shape *','shape_name',initial.shapes.map(s=>[s.name,s.name+' · '+s.cpu_core_count+' cores · '+s.memory_size_in_gbs+' GB']),db.shape_name)}${select('Target configuration *','configuration_id',[['','Select a target configuration'],...configurationChoices(initial.configurations)],db.configuration_id)}${select('Promoted HA configuration (optional for original shape)','promoted_ha_configuration_id',[['','Reuse original HA configuration when the shape is unchanged'],...configurationChoices(initial.ha_configurations)])}<p class="small muted" id="target-configuration-info">Target and promoted HA configurations must match the target shape. A promoted HA configuration is optional when keeping the original shape; choose one when changing shape.</p>${field('Target storage (GB)','data_storage_size_in_gbs',db.data_storage_size_in_gbs,'number')}${select('Secret compartment *','secret_compartment_id',compartments.map(c=>[c.id,c.name]),inventory.compartment_id)}${field('Blue-green replication username','replication_username','','text',true)}${select('Replication password secret *','replication_secret_id',secretOptions(c))}</div><button type="button" id="new-secret">Create a Vault secret</button>${db.is_highly_available?`<div class="form-section"><h3>HA conversion</h3><p class="small muted">Blue HA is disabled for native creation. Green stays standalone.</p>${check('Restore Blue HA after creation (option 2)','restore_blue_ha')}${select('Blue HA configuration','blue_ha_configuration_id',configurationChoices(initial.ha_configurations),db.configuration_id)}</div>`:''}<div class="form-section"><h3>Inbound channel registration</h3><p class="small muted">Each inbound channel is copied to Green disabled. These channels are separate from the native Blue-to-Green channel.</p>${channels.map((ch,i)=>`<div class="form-grid"><div><strong>${esc(ch.display_name)}</strong>${field('Replication username',`channel-user-${i}`,ch.source?.username||'','text',true)}</div>${select('Password secret *',`channel-secret-${i}`,secretOptions(c))}</div>`).join('')||'<p class="small muted">No inbound replication channels.</p>'}</div><div class="form-section"><h3>Source scheduled events · review required</h3><div id="creation-events">${eventsMarkup(review,'create')}</div>${check('I reviewed the event list and disable-before choices','events_reviewed')}</div>${footer('Review creation')}</form>`);
 let targetRevision=0,targetReady=Boolean(initial.configurations.length && initial.versions.length);
 const targetForm=$('#modal-body form'),reviewButton=targetForm.querySelector('button[type=submit]');
 ['mysql_version','shape_name','configuration_id'].forEach(name=>targetForm.elements[name].required=true);
 reviewButton.disabled=!targetReady||mutationsDisabled;
 targetForm.elements.shape_name.addEventListener('change',async()=>{
  const revision=++targetRevision,shape=targetForm.elements.shape_name.value;
  targetReady=false;reviewButton.disabled=true;
  const config=targetForm.elements.configuration_id,promoted=targetForm.elements.promoted_ha_configuration_id;
  config.innerHTML='<option value="">Loading configurations…</option>';promoted.innerHTML='<option value="">Loading HA configurations…</option>';
  config.disabled=true;promoted.disabled=true;
  try {
   const options=await api(`/api/db/${encodeURIComponent(id)}/creation-options?shape_name=${encodeURIComponent(shape)}`);
   if(revision!==targetRevision||!targetForm.isConnected)return;
   config.innerHTML=[['','Select a target configuration'],...configurationChoices(options.configurations)].map(([value,label])=>`<option value="${esc(value)}">${esc(label)}</option>`).join('');
   const sameShape=shape===db.shape_name;
   promoted.innerHTML=[['',sameShape?'Reuse original HA configuration (optional override)':'Select a promoted HA configuration for '+shape],...configurationChoices(options.ha_configurations)].map(([value,label])=>`<option value="${esc(value)}">${esc(label)}</option>`).join('');
   if(options.configurations.some(c=>c.id===db.configuration_id))config.value=db.configuration_id;
   promoted.required=!sameShape;
   promoted.parentElement.firstChild.textContent=sameShape?'Promoted HA configuration (optional override)':'Promoted HA configuration *';
   targetReady=Boolean(options.configurations.length && (sameShape||options.ha_configurations.length));
   setText('#target-configuration-info',options.information.join(' ')|| (sameShape?'Original shape retained. Promoted HA configuration is optional; the original HA configuration can be reused.':'Choose target and promoted HA configurations matching '+shape+'.'));
  }catch(error){if(revision===targetRevision && targetForm.isConnected){setText('#target-configuration-info','Configuration discovery unavailable: '+error.message);}}
  finally{if(revision===targetRevision && targetForm.isConnected){config.disabled=false;promoted.disabled=false;reviewButton.disabled=!targetReady||mutationsDisabled;}}
 });
 $('[name=secret_compartment_id]').addEventListener('change',async()=>{try{const c=await catalog($('[name=secret_compartment_id]').value);document.querySelectorAll('[name=replication_secret_id],[name^=channel-secret-]').forEach(s=>s.innerHTML=secretOptions(c).map(o=>`<option value="${esc(o[0])}">${esc(o[1])}</option>`).join(''));}catch(e){modalError(e);}});
 $('#new-secret').addEventListener('click',()=>secretDialog().catch(modalError));
 submitForm(async (f,form)=>{
  if(!targetReady)throw Error('Wait for target configurations to be verified');
  if(!f.get('events_reviewed'))throw Error('Review scheduled events and confirm your choices');
  const params=Object.fromEntries(f);params.source_id=id;params.restore_blue_ha=Boolean(f.get('restore_blue_ha'));
  params.event_review=eventChoices($('#creation-events'),review,'create');
  params.channel_credentials=Object.fromEntries(channels.map((ch,i)=>[ch.id,{username:f.get(`channel-user-${i}`),secret_id:f.get(`channel-secret-${i}`)}]));
  await queued('/api/create',params);
 });
}
async function secretDialog() {
 const compartments=await api('/api/compartments');
 modal('Create OCI Vault secret',`<form>${select('Secret compartment','compartment_id',compartments.map(c=>[c.id,c.name]),inventory.compartment_id)}<div id="vault-select"></div><div id="key-select"></div>${field('Secret name','name','','text',true)}${field('Secret value','password','','password',true)}${footer('Review secret creation')}</form><p class="small muted">Return to creation and choose the new secret after it becomes active.</p>`);
 async function loadVaults() {
  const comp=$('[name=compartment_id]').value, c=await catalog(comp);
  $('#vault-select').innerHTML=select('Vault *','vault_id',[['','Select a Vault'],...c.vaults.map(v=>[v.id,v.display_name])]);
  $('#key-select').innerHTML='';
  $('[name=vault_id]').addEventListener('change',async()=>{try{const keys=await api(`/api/keys?vault_id=${encodeURIComponent($('[name=vault_id]').value)}&compartment_id=${encodeURIComponent(comp)}`);$('#key-select').innerHTML=select('Encryption key *','key_id',[['','Select a key'],...keys.filter(k=>k.algorithm?.algorithm==='AES').map(k=>[k.id,k.display_name])]);}catch(e){modalError(e);}});
 }
 $('[name=compartment_id]').addEventListener('change',()=>loadVaults().catch(modalError));await loadVaults();
 submitForm(async f=>queued('/api/secrets',Object.fromEntries(f),v=>message('Secret created: '+v.secret_name+'. Reopen environment creation after it becomes active.')));
}
async function switchDialog() {
 await showDetail(selected);
 if(!detailData?.actions?.switchover?.allowed)throw Error(detailData?.actions?.switchover?.reason||'Switchover is unavailable');
 const d=detailData.deployment;
 const [source,target]=await Promise.all([api('/api/events/'+encodeURIComponent(d.source_db_system_id)),api('/api/events/'+encodeURIComponent(d.target_db_system_id))]);
 const originals=detailData.plan.original_events || source.events;
 const haCatalog=await api(`/api/db/${encodeURIComponent(d.target_db_system_id)}/ha-configurations`);
 modal('Switchover · event and promotion choices',`<form><p class="notice">Switchover moves the primary endpoint and may interrupt connections. Blue HA is disabled, Green becomes read-only for the handoff, and promoted Green must pass read-write and GTID verification.</p><div class="form-grid">${field('Replication drain wait (seconds)','wait_seconds',300,'number',true)}${select('Promoted HA configuration (optional override)','ha_configuration_id',[['','Reuse the registered / original HA configuration'],...haCatalog.configurations.map(c=>[c.id,c.display_name+' · '+c.id])])}</div><p class="small muted">Leave the HA configuration blank to reuse ${esc(detailData.promotion_ha_default?.source||'the existing configuration')}${detailData.promotion_ha_default?.configuration_id?': '+esc(detailData.promotion_ha_default.configuration_id):''}. Enter an OCID only to override it. HA is enabled only when selected below.</p>${check('Enable HA on promoted Green','enable_ha')}${check('Activate registered copied inbound channels','activate_channels')}<div class="form-section"><h3>Source event handling before switchover</h3><div id="source-events">${eventsMarkup(source,'source')}</div></div><div class="form-section"><h3>Green final event states after switchover</h3><div id="target-events">${eventsMarkup(target,'target',originals)}</div></div>${check('I reviewed event states and the endpoint handoff','events_reviewed')}${footer('Review switchover')}</form>`);
 submitForm(async f=>{if(!f.get('events_reviewed'))throw Error('Review event choices before switchover');await queued(`/api/deployments/${encodeURIComponent(d.id)}/action`,{action:'switchover',wait_seconds:Number(f.get('wait_seconds')),ha_configuration_id:f.get('ha_configuration_id'),enable_ha:Boolean(f.get('enable_ha')),activate_channels:Boolean(f.get('activate_channels')),source_event_review:eventChoices($('#source-events'),source,'source'),target_event_review:eventChoices($('#target-events'),target,'target')});});
}
async function configurationDialog(id,focusVariable=null){
 const data=await api(`/api/db/${encodeURIComponent(id)}/configuration-editor`),draft={...data.variables};
 let page=1,query=focusVariable||'',removed=new Set();const size=10;
 modal('Edit HeatWave configuration',`<p><strong>${esc(data.source.display_name)}</strong> <span class="small resource-id">[${esc(data.source.id)}]</span></p><p class="small muted">Shape: ${esc(data.source.shape_name)}. Only configured values are listed. Save creates a new version; applying it to this DB system requires a separate confirmation.</p><form id="configuration-form"><p>Save as <strong id="configuration-version">${esc(data.suggested_name)}</strong></p><div class="form-grid"><label>Search configured variables<input id="configuration-search" type="search"></label><label>Add HeatWave variable<select id="configuration-add" aria-label="Add HeatWave variable"></select></label></div><div class="table-wrap"><table class="configuration-editor"><thead><tr><th>Variable</th><th>Value</th><th>Action</th></tr></thead><tbody id="configuration-rows"></tbody></table></div><div class="pagination"><span id="configuration-page" class="small muted"></span><div><button type="button" id="configuration-previous">Previous</button><button type="button" id="configuration-next">Next</button></div></div><div id="configuration-removal"></div><p id="configuration-removed" class="small orange"></p>${footer('Review new configuration')}</form><details><summary>Service-managed and initialization values (read-only)</summary>${facts(Object.entries({...data.read_only_variables,...data.initialization_variables}).filter(([,value])=>value!==null))}</details><div class="form-section"><h3>Apply an existing configuration</h3>${select('Configuration for this shape','apply_configuration_id',[['','Select a configuration'],...data.configurations.map(c=>[c.id,c.display_name+' ['+c.id+']'])])}<button type="button" id="configuration-apply" ${mutationsDisabled?'disabled':''}>Review application to DB system</button></div><p class="small muted">Supported variables reference: <a href="${esc(data.catalog_source)}" target="_blank" rel="noopener">Oracle HeatWave</a> (${esc(data.catalog_reviewed)}). OCI validates version and shape-specific limits.</p>`);
 const form=$('#configuration-form');
 function control(name,value){
  const spec=data.catalog[name],attr=`data-config-value="${esc(name)}" aria-label="${esc(name)} value"`;
  if(spec.type==='enum')return `<select ${attr}>${[...new Set([value,...spec.choices])].map(v=>`<option value="${esc(v)}" ${v===value?'selected':''}>${esc(v||'(empty)')}</option>`).join('')}</select>`;
  if(spec.type==='set'){const selected=value?value.split(',').map(v=>v.trim()):[];return `<select ${attr} multiple size="4">${[...new Set([...selected,...spec.choices])].filter(v=>v!=='').map(v=>`<option value="${esc(v)}" ${selected.includes(v)?'selected':''}>${esc(v)}</option>`).join('')}</select><p class="small muted">Select one or more. No selection saves an empty set.</p>`;}
  if(spec.type==='flags'){const flags=Object.fromEntries(value.split(',').filter(Boolean).map(v=>v.split('=')));return `<details><summary>Edit flags</summary>${[...new Set([...Object.keys(flags),...spec.choices])].map(flag=>`<label>${esc(flag)}<select data-config-flag="${esc(name)}" data-flag="${esc(flag)}" aria-label="${esc(name+' '+flag)}"><option value="">Unspecified</option>${['on','off'].map(v=>`<option ${flags[flag]===v?'selected':''}>${v}</option>`).join('')}</select></label>`).join('')}</details>`;}
  return `<input ${attr} value="${esc(value)}" type="text" ${['integer','decimal'].includes(spec.type)?'inputmode="decimal"':''}><span class="small muted">${esc(spec.type)}${spec.min!==undefined?' · '+esc(spec.min)+' to '+esc(spec.max):''}</span>`;
 }
 function render(){
  const names=Object.keys(draft).sort().filter(name=>`${name} ${draft[name]}`.toLowerCase().includes(query));
  const pages=Math.max(1,Math.ceil(names.length/size));page=Math.min(page,pages);
  $('#configuration-rows').innerHTML=names.slice((page-1)*size,page*size).map(name=>`<tr><th scope="row">${esc(name)}</th><td>${control(name,draft[name])}</td><td><button type="button" data-remove-variable="${esc(name)}" ${data.catalog[name].removable?'':'disabled title="HeatWave requires this variable; edit its value instead"'}>Remove</button></td></tr>`).join('')||'<tr><td colspan="3">No matching configured variables.</td></tr>';
  $('#configuration-add').innerHTML='<option value="">Select a configurable variable to add</option>'+Object.keys(data.catalog).filter(name=>!(name in draft)).sort().map(name=>`<option>${esc(name)}</option>`).join('');
  setText('#configuration-page',`${names.length} configured variables · Page ${page} of ${pages}`);setProperty('#configuration-previous','disabled',page===1);setProperty('#configuration-next','disabled',page===pages);
  setText('#configuration-removed',removed.size?'Omitted from this version: '+[...removed].join(', ')+'. '+data.removal_warning:'');
 }
 form.addEventListener('input',e=>{if(e.target.dataset.configValue)draft[e.target.dataset.configValue]=e.target.value;});
 form.addEventListener('change',e=>{
  const name=e.target.dataset.configValue;
  if(name)draft[name]=e.target.multiple?[...e.target.selectedOptions].map(o=>o.value).join(','):e.target.value;
  const flagged=e.target.dataset.configFlag;
  if(flagged)draft[flagged]=[...form.querySelectorAll('[data-config-flag]')].filter(el=>el.dataset.configFlag===flagged && el.value).map(el=>el.dataset.flag+'='+el.value).join(',');
 });
 $('#configuration-search').addEventListener('input',e=>{query=e.target.value.toLowerCase();page=1;render();});
 $('#configuration-previous').addEventListener('click',()=>{page--;render();});$('#configuration-next').addEventListener('click',()=>{page++;render();});
 $('#configuration-add').addEventListener('change',e=>{const name=e.target.value;if(!name)return;draft[name]=data.variables[name]??'';removed.delete(name);query=name;$('#configuration-search').value=name;page=1;render();});
 form.addEventListener('click',e=>{
  const name=e.target.closest('[data-remove-variable]')?.dataset.removeVariable;if(!name)return;
  $('#configuration-removal').innerHTML=`<div class="notice"><strong>Remove ${esc(name)}?</strong><p>${esc(data.removal_warning)}</p><button type="button" id="confirm-variable-removal">Remove from new version</button> <button type="button" id="cancel-variable-removal">Keep variable</button></div>`;
  $('#cancel-variable-removal').addEventListener('click',()=>$('#configuration-removal').replaceChildren());
  $('#confirm-variable-removal').addEventListener('click',()=>{delete draft[name];if(name in data.variables)removed.add(name);$('#configuration-removal').replaceChildren();render();});
 });
 $('#configuration-apply').addEventListener('click',async()=>{try{const config=$('[name=apply_configuration_id]').value;if(!config)throw Error('Select a configuration to apply');await queued(`/api/db/${encodeURIComponent(id)}/action`,{action:'configuration',configuration_id:config});}catch(error){modalError(error);}});
 submitForm(async()=>queued(`/api/db/${encodeURIComponent(id)}/configuration-versions`,{source_configuration_id:data.source.id,display_name:data.suggested_name,variables:draft,acknowledge_removal:removed.size>0},result=>configurationSaved(id,result.job_id)));
 $('#configuration-search').value=query;render();
}
async function configurationSaved(dbId,jobId){
 const compartment=inventory.compartment_id;
 modal('Saving configuration',`<p id="configuration-save-status">The configuration is being created. You can close this window and find it later in the configuration editor.</p>`);
 const marker=$('#configuration-save-status');
 for(let attempt=0;attempt<120;attempt++){
  if(!marker.isConnected||!$('#modal').open||inventory.compartment_id!==compartment)return;
  const result=await api('/api/configuration-result/'+encodeURIComponent(jobId));
  if(result.status==='SUCCEEDED' && result.configuration?.id){
   const config=result.configuration;
   modal('Configuration saved',`<p><strong>${esc(config.display_name)}</strong></p><p class="small resource-id">${esc(config.id)}</p><p>Apply this configuration to the selected DB system now? Applying can restart the DB system and interrupt connections.</p><div class="form-footer"><button id="configuration-later">Keep saved · apply later</button><button id="configuration-apply-saved" class="primary">Review application to DB system</button></div>`);
   $('#configuration-later').addEventListener('click',()=>$('#modal').close());
   $('#configuration-apply-saved').addEventListener('click',()=>queued(`/api/db/${encodeURIComponent(dbId)}/action`,{action:'configuration',configuration_id:config.id}).catch(modalError));return;
  }
  if(!['QUEUED','RUNNING','ACCEPTED','IN_PROGRESS'].includes(result.status)){setText(marker,'Configuration creation '+result.status.toLowerCase()+'. See Activity history for details.');return;}
  await new Promise(resolve=>setTimeout(resolve,2000));
 }
 setText(marker,'Creation is still running. Follow it in Activity history and reopen the configuration editor when complete.');
}

async function enableHADialog(id) {
 const catalog=await api(`/api/db/${encodeURIComponent(id)}/ha-configurations`);
 const current=catalog.configurations.some(c=>c.id===catalog.current_configuration_id)?catalog.current_configuration_id:'';
 const options=[['','Select an HA configuration'],...catalog.configurations.map(c=>[c.id,c.display_name+' · '+c.id+(c.id===catalog.current_configuration_id?' · current':'')])];
 modal('Enable HA · configuration',`<form><p class="small muted">Only ACTIVE HA configurations for shape <strong>${esc(catalog.shape_name)}</strong> are listed. Custom configurations are confined to the selected compartment. The current HA configuration is preselected when compatible.</p>${select('HA configuration','configuration_id',options,current)}${catalog.information.map(value=>`<div class="information">${esc(value)}</div>`).join('')}${!catalog.configurations.length?'<div class="information">No compatible HA configurations could be verified for this DB-system shape. Review configuration read access in the selected compartment.</div>':''}${footer('Review HA changes')}</form>`);
 $('#modal [name=configuration_id]').required=true;
 if(!catalog.configurations.length)$('#modal button[type=submit]').disabled=true;
 submitForm(async f=>queued(`/api/db/${encodeURIComponent(id)}/action`,{action:'ha_on',configuration_id:f.get('configuration_id')}));
}
async function confirmAction(title,text,path,body,extra='') {
 if(!extra)return queued(path,body);
 modal(title,`<form><p>${esc(text)}</p>${extra}${footer('Review changes')}</form>`);
 submitForm(async f=>queued(path,{...body,...Object.fromEntries(f)}));
}
$('#close-modal').addEventListener('click',()=>$('#modal').close());
$('#view-all-activity').addEventListener('click',()=>$('[data-tab=history]').click());
$('#notifications').addEventListener('click',()=>{if(failedNotification)currentNotification=failedNotification;paintNotification(true);});
$('#dismiss-message').addEventListener('click',()=>{
 failedNotification=null;
 if(currentNotification.color==='red'){
  const running=inventory?.history.find(j=>['QUEUED','ACCEPTED','RUNNING','IN_PROGRESS'].includes(j.status));
  currentNotification=running?jobNotification(running):{text:'No unacknowledged failures.',color:'green',title:'Notifications acknowledged'};
 }
 paintNotification();setProperty('#message','hidden',true);
});
$('#message-history').addEventListener('click',()=>{
 $('[data-tab=history]').click();
 const job=currentNotification.jobId&&$('#history-list [data-resource-id="'+CSS.escape(currentNotification.jobId)+'"]');
 if(job)job.scrollIntoView({behavior:'smooth',block:'start'});
});
$('#refresh').addEventListener('click',refresh);
$('#search').addEventListener('input',()=>renderFleet());
['state-filter','channel-filter','replica-filter'].forEach(id=>$('#'+id).addEventListener('change',()=>{renderFleet();if(inspected)showSystem(inspected);}));
$('#sort-name').addEventListener('click',()=>{descending=!descending;renderFleet();});
$('#new-deployment').addEventListener('click',()=>createDialog().catch(e=>message(e.message,'red')));
$('#switchover').addEventListener('click',()=>switchDialog().catch(e=>message(e.message,'red')));
$('#delete-deployment').addEventListener('click',()=>queued(`/api/deployments/${encodeURIComponent(selected)}/action`,{action:'delete'}).catch(e=>message(e.message,'red')));
$('#precheck').addEventListener('click',()=>confirmAction('Switchover precheck','Check GTIDs, standalone Green and fresh native replication health.',`/api/deployments/${encodeURIComponent(selected)}/action`,{action:'precheck'}));
$('#activate-copies').addEventListener('click',()=>confirmAction('Activate copied channels','Enable registered inbound channels on promoted Green and verify health.',`/api/deployments/${encodeURIComponent(selected)}/action`,{action:'activate_copies'}));
$('#change-compartment').addEventListener('click',async()=>{try{const c=await api('/api/compartments');modal('Change compartment',`<form>${select('Compartment','compartment_id',c.map(x=>[x.id,x.name]),inventory.compartment_id)}<div class="form-footer"><button class="primary">Use compartment</button></div></form>`);submitForm(async f=>{await api('/api/compartment',Object.fromEntries(f));selected=null;inspected=null;systemDetailVersion++;$('#selected').hidden=true;$('#modal').close();await refresh();});}catch(e){message(e.message,'red');}});
document.addEventListener('click',async e=>{
 const button=e.target.closest('button');if(!button)return;
 try{
  if(button.dataset.tab){document.querySelectorAll('[data-tab]').forEach(b=>{const active=b===button;b.classList.toggle('active',active);b.setAttribute('aria-selected',active);});document.querySelectorAll('.tab-panel').forEach(p=>p.hidden=p.id!==button.dataset.tab);}
  if(button.dataset.fleet){fleet=button.dataset.fleet;document.querySelectorAll('[data-fleet]').forEach(b=>{const active=b===button;b.classList.toggle('active',active);b.setAttribute('aria-selected',active);});renderFleet();}
  if(button.dataset.detailTab)activateDetailTab(button.closest('[data-tabview]'),button.dataset.detailTab);
  if(button.dataset.tab)for(const root of document.querySelectorAll('[data-tabview]'))void refreshSqlTab(root);
  if(button.dataset.sqlPage){const root=button.closest('[data-tabview]'),kind=button.dataset.sqlKind,state=sqlState(root,kind);state.page+=button.dataset.sqlPage==='next'?1:-1;renderSqlTable(root,kind);}
  if(button.dataset.workPage){const key=button.dataset.pageKey,state=requestPages.get(key);if(state){state.page+=button.dataset.workPage==='next'?1:-1;redrawRequestPages(key);}}
  if(button.dataset.inspect)await showSystem(button.dataset.inspect);
  if(button.dataset.relatedDb){
   const target=inventory.systems.find(s=>s.id===button.dataset.relatedDb);
   if(!target)throw Error('Refresh the dashboard to load this paired DB system');
   fleet=target.is_highly_available?'ha':'standalone';
   document.querySelectorAll('[data-fleet]').forEach(b=>{const active=b.dataset.fleet===fleet;b.classList.toggle('active',active);b.setAttribute('aria-selected',active);});
   if(!`${target.display_name} ${target.id} ${target.ip_address||''}`.toLowerCase().includes($('#search').value.toLowerCase()))$('#search').value='';
   if($('#state-filter').value && $('#state-filter').value!==target.lifecycle_state)$('#state-filter').value='';
   if($('#channel-filter').value && !target.channels.some(channelMatches))$('#channel-filter').value='';
   if($('#replica-filter').value && ($('#replica-filter').value==='present')!==(target.read_replicas.length>0))$('#replica-filter').value='';
   await showSystem(target.id);
  }
  if(button.dataset.select)await showDetail(button.dataset.select);
  if(button.dataset.create)await createDialog(button.dataset.create);
  if(button.dataset.configure)await configurationDialog(button.dataset.configure);
  if(button.dataset.observe)await observerDialog(button.dataset.observe);
  if(button.dataset.events)await manageEvents(button.dataset.events);
  if(button.dataset.work){
   try{showWorkRequest(await api('/api/work-requests/'+encodeURIComponent(button.dataset.work)));}
   catch(error){modal('Work request status unavailable',`<div class="information"><p>${esc(error.message)}</p><p>This detail request could not verify the OCI status. It does not establish that the deployment operation failed. Check its workflow journal and compartment-specific access.</p></div>`);message(error.message,'orange',{title:'Work-request status unavailable'});}
  }
  if(button.dataset.db){if(button.dataset.action==='ha_on')await enableHADialog(button.dataset.db);else await confirmAction('Update DB system','Apply '+button.dataset.action.replaceAll('_',' ')+' to the selected DB system.',`/api/db/${encodeURIComponent(button.dataset.db)}/action`,{action:button.dataset.action});}
  if(button.dataset.channel){
   const path=`/api/channels/${encodeURIComponent(button.dataset.channel)}/action`,params={action:button.dataset.action};
   if(button.dataset.action==='refresh_channel_status')await queued(path,params);
   else await confirmAction('Replication channel action',button.dataset.action==='delete_channel'?'Delete this channel permanently?':'Apply '+button.dataset.action.replaceAll('_',' ')+' to this channel.',path,params);
  }
  if(button.hasAttribute('data-all-events'))button.parentElement.querySelectorAll('input[type=checkbox]:not(:disabled)').forEach(c=>c.checked=true);
 }catch(err){if($('#modal').open)modalError(err);else message(err.message,'red');}
});
document.addEventListener('input',e=>{const kind=e.target.dataset.sqlSearch;if(kind){const root=e.target.closest('[data-tabview]'),state=sqlState(root,kind);state.search=e.target.value;state.page=1;renderSqlTable(root,kind);}});
document.addEventListener('change',e=>{const kind=e.target.dataset.sqlSize;if(kind){const root=e.target.closest('[data-tabview]'),state=sqlState(root,kind);state.size=Number(e.target.value);state.page=1;renderSqlTable(root,kind);}const key=e.target.dataset.pageSize;if(key){const state=requestPages.get(key);if(state){state.size=Number(e.target.value);state.page=1;redrawRequestPages(key);}}});
document.addEventListener('keydown',e=>{if(!e.target.matches('[data-detail-tab]')||!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;const root=e.target.closest('[data-tabview]'),tabs=[...root.querySelectorAll('[data-detail-tab]')],index=tabs.indexOf(e.target);const next=e.key==='Home'?0:e.key==='End'?tabs.length-1:(index+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;e.preventDefault();activateDetailTab(root,tabs[next].dataset.detailTab,true);});
refresh();setInterval(refresh,15000);
