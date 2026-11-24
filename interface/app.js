'use strict';
const $=id=>document.getElementById(id),form=$('form');
let plan=null,coupled=null,dirty=true,revision=0,activeJob=null,loading=false,profile=null;
const coupledMode=()=>$('sim-mode').value==='acoplado';
const fmt=(v,n=2)=>Number(v).toLocaleString('pt-BR',{minimumFractionDigits:n,maximumFractionDigits:n});
const money=v=>'R$ '+fmt(v);
function message(text,error=false){$('message').textContent=text;$('message').className=error?'error':'';}
function row(parent,values){const tr=document.createElement('tr');values.forEach(v=>{const td=document.createElement('td');if(v instanceof Node)td.append(v);else td.textContent=v;tr.append(td);});parent.append(tr);return tr;}
function changed(){updateStockHints();revision++;dirty=true;$('download-plan').disabled=true;$('download-coupled').disabled=true;$('result').classList.add('stale');$('planning').classList.add('stale');$('coupled').classList.add('stale');message('Parâmetros alterados. Calcule novamente para atualizar os resultados.');}
function scenario(){
  const data=Object.fromEntries(new FormData(form));
  Object.keys(data).forEach(k=>{if(!['objetivo','data_inicio'].includes(k))data[k]=data[k]===''?null:Number(data[k]);});
  data.padrao=$('standard').value;data.raca='HO';
  data.precos_custom={};data.alimentos_bloqueados=[];data.estoques={};
  document.querySelectorAll('#foods tr').forEach(tr=>{
    const id=Number(tr.dataset.id);
    data.precos_custom[id]=parseDecimal(tr.querySelector('.price').value,'Preço de '+tr.dataset.name);
    const stock=parseDecimal(tr.querySelector('.stock').value,'Estoque de '+tr.dataset.name,true);
    if(stock!==null)data.estoques[id]=stock;
    if(!tr.querySelector('[type=checkbox]').checked)data.alimentos_bloqueados.push(id);
  });
  data.premix_composicao=premixComposition();
  return data;
}
function premixComposition(){
  const text=$('premix-json').value.trim();if(!text)return {};
  try{return JSON.parse(text);}catch(e){throw Error('Garantia do premix: use JSON válido, por exemplo {"Ca":150,"P":60}, ou deixe o campo vazio.');}
}
function coupledScenario(){
  const full=scenario(),optional=v=>v===null||v===undefined?null:v;
  const seed=$('semente').value.trim();
  if(!/^\d+$/.test(seed))throw Error('Semente aleatória: informe um inteiro não negativo.');
  return {padrao:full.padrao,dias_simulacao:full.dias_simulacao,intervalo_dieta:full.intervalo_dieta,
    preco_leite_kg:full.preco_leite_kg,teto_caroco:optional(full.teto_caroco),orcamento_dia:optional(full.orcamento_dia),
    precos_custom:full.precos_custom,alimentos_bloqueados:full.alimentos_bloqueados,premix_composicao:full.premix_composicao,semente:Number(seed),
    otimizador:$('optimizer').value};
}
async function request(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.mensagem||'Falha na solicitação.');return d;}
function renderDiet(data){
  $('result').hidden=false;$('result').classList.remove('stale');
  $('cost').textContent=money(data.custo_total_dia);$('margin').textContent=money(data.iofc_margem_dia);$('dmi').textContent=fmt(data.consumo_total_ms)+' kg';
  $('coverage').textContent=data.motor+' · '+data.padrao+' · '+data.iteracoes+' iterações · '+data.restricoes.length+' restrições verificadas · '+(data.rotulo_leite||'Meta')+' '+fmt(data.leite_meta)+' kg/vaca/dia';
  ['diet','constraints','warnings','history'].forEach(id=>$(id).replaceChildren());
  (data.historico||[]).forEach(h=>row($('history'),[h.iteracao,money(h.custo),fmt(h.consumo_ms,3),fmt(h.amido_pct,3),h.ed_amido_mcal_kg_amido==null?'Não calculada no NRC':fmt(h.ed_amido_mcal_kg_amido,4),fmt(Math.max(0,-h.menor_residuo),6)]));
  data.ingredientes.forEach(i=>row($('diet'),[i.nome,fmt(i.kg_ms,3),fmt(i.kg_natural,3),money(i.custo_dia)]));
  data.restricoes.forEach(r=>row($('constraints'),[r.nome,fmt(r.folga,6)+' '+r.unidade,r.atendida?'Atendida':'Violada']));
  (data.avisos||plan?.avisos||[]).forEach(a=>{const li=document.createElement('li');li.textContent=a;$('warnings').append(li);});
  $('export').hidden=true;
}
function renderPlan(data){
  plan=data;$('planning').hidden=false;$('planning').classList.remove('stale');
  $('milk-total').textContent=fmt(data.leite_total_kg,0)+' kg';$('cost-total').textContent=money(data.custo_total);$('margin-total').textContent=money(data.margem_total);
  $('plan-summary').textContent=data.cenario.dias_simulacao+' dias · '+data.cenario.vacas_lactacao+' lactantes / '+data.cenario.tamanho_rebanho+' animais · Holandesa HO · '+data.motor+' · '+data.padrao;
  $('periods').replaceChildren();$('daily').replaceChildren();
  data.periodos.forEach(p=>{
    const btn=document.createElement('button');btn.type='button';btn.className='secondary';btn.textContent='Examinar';btn.addEventListener('click',()=>renderDiet(p.resultado));
    row($('periods'),[p.inicio+'–'+p.fim,fmt(p.resultado.leite_meta),money(p.resultado.custo_total_dia),btn]);
    p.diario.forEach(d=>row($('daily'),[d.data+' ('+d.dia+')',d.del_dias,fmt(d.leite_vaca),fmt(d.leite_rebanho),money(d.custo_rebanho)]));
  });renderDiet(data.periodos[0].resultado);$('download-plan').disabled=false;
}
function eventDiet(data,e){
  return {motor:data.motor,padrao:data.padrao,iteracoes:e.iteracoes,restricoes:e.restricoes,leite_meta:e.leite_medio,
    rotulo_leite:'Dia '+e.dia+' · baia '+e.baia+' · leite médio',custo_total_dia:e.custo_vaca_dia,
    iofc_margem_dia:e.leite_medio*data.cenario.preco_leite_kg-e.custo_vaca_dia,consumo_total_ms:e.consumo_ms,
    ingredientes:e.dieta,historico:[],avisos:data.avisos};
}
function checkText(c){
  if(!c)return 'Sem dieta do otimizador';if(c.atende)return 'Atende';
  return 'Falta: '+c.falhas.map(f=>f.nome+' '+fmt(-f.folga,2)+' '+f.unidade).join('; ');
}
function renderCoupled(data){
  coupled=data;$('coupled').hidden=false;$('coupled').classList.remove('stale');
  const r=data.resumo;
  $('c-milk').textContent=fmt(r.leite_total_kg,0)+' kg';$('c-cost').textContent=money(r.custo_total);$('c-margin').textContent=money(r.margem_total);
  $('c-summary').textContent=r.otimizador+' · '+r.dias_simulados+' dias simulados a partir de '+data.inicio+' · '+fmt(r.media_vacas_lactacao,1)+' vacas em lactação em média · '+
    fmt(r.media_leite_vaca,1)+' kg/vaca/dia · '+money(r.custo_por_kg_leite||0)+'/kg de leite · '+r.reformulacoes+' reformulações · '+data.padrao+
    (r.dias_conferidos?' · '+r.dias_exigencia_nao_atendida+' de '+r.dias_conferidos+' dias com alguma exigência média não atendida':'');
  $('c-events').replaceChildren();$('c-daily').replaceChildren();
  data.reformulacoes.forEach(e=>{
    const btn=document.createElement('button');btn.type='button';btn.className='secondary';btn.textContent='Examinar';
    if(e.sucesso)btn.addEventListener('click',()=>renderDiet(eventDiet(data,e)));else btn.disabled=true;
    row($('c-events'),[e.data+' (dia '+e.dia+')',e.vacas,fmt(e.leite_medio,1),fmt(e.corte_leite_medio,2),fmt(e.del_medio,0),money(e.custo_vaca_dia),
      e.sucesso?'Nova dieta':'Sem dieta nova: anterior mantida',btn]);
  });
  data.diario.forEach(d=>row($('c-daily'),[d.data+' ('+d.dia+')',d.vacas_lactacao,fmt(d.leite_medio,1),fmt(d.leite_rebanho,0),money(d.custo_rebanho),checkText(d.conferencia)]));
  $('c-folder').textContent='Entradas, logs e resultado desta rodada: '+data.pasta;
  const first=data.reformulacoes.find(e=>e.sucesso);if(first)renderDiet(eventDiet(data,first));
  $('download-coupled').disabled=false;
}
function showDiagnostics(data){
  const box=$('diagnostic');box.replaceChildren();box.hidden=false;
  const heading=document.createElement('h2');heading.textContent='Não foi possível concluir este cenário';box.append(heading);
  const info=document.createElement('p');info.textContent=data.mensagem||'Revise os limites e tente novamente.';box.append(info);
  const lines=data.violacoes||(data.restricoes||[]).filter(c=>c.folga < -1e-5*c.escala).slice(0,8);
  if(lines.length){const ul=document.createElement('ul');lines.forEach(c=>{const li=document.createElement('li');li.textContent=c.nome+': falta '+fmt(-c.folga,4)+' '+(c.unidade||'');ul.append(li);});box.append(ul);}
  if(data.limites){const detail=document.createElement('details');const summary=document.createElement('summary');summary.textContent='Limites por vaca/dia após estoque e exclusões';detail.append(summary);data.limites.forEach(l=>{const p=document.createElement('p');p.textContent=l.nome+': até '+fmt(l.kg_ms_vaca_dia,4)+' kg MS/vaca/dia';detail.append(p);});box.append(detail);}
  if(data.detalhe_solver){const p=document.createElement('p');p.textContent='Diagnóstico do solver: '+data.detalhe_solver;box.append(p);}
}
function setBusy(busy){
  document.querySelectorAll('#form input,#form select,#foods input,#premix-json,#reset').forEach(el=>el.disabled=busy);
  $('solve').disabled=busy;$('cancel').hidden=!busy;
}
function updateStockHints(){
  const n=Number(form.elements.vacas_lactacao.value),days=Number(form.elements.dias_simulacao.value);
  document.querySelectorAll('#foods tr').forEach(tr=>{const help=tr.querySelector('.stock-help');if(!help)return;try{const v=parseDecimal(tr.querySelector('.stock').value,'Estoque',true);help.textContent=v===null?'Sem limite de estoque':n>0&&days>0?'Até '+fmt(v/(n*days),4)+' kg MS/vaca/dia':'';}catch(e){help.textContent='Confira o número informado.';}});
}
async function poll(id,token){
  for(;;){
    await new Promise(r=>setTimeout(r,1000));
    const response=await fetch('/api/rufas/jobs/'+id);if(!response.ok)throw Error('O cálculo foi perdido. Verifique o servidor.');
    const job=await response.json();message(job.progresso+' · '+fmt(job.segundos_decorridos||0,0)+'s');
    if(['concluido','falhou','cancelado'].includes(job.estado)){
      if(token!==revision)throw Error('Parâmetros alterados durante o cálculo.');
      if(!job.resultado.sucesso){if(job.estado!=='cancelado')showDiagnostics(job.resultado);throw Error(job.resultado.mensagem);}
      return job.resultado;
    }
  }
}
$('cancel').addEventListener('click',async()=>{if(activeJob){$('cancel').disabled=true;try{await request('/api/rufas/jobs/'+activeJob+'/cancel',{});message('Cancelando cálculo…');}catch(e){message(e.message,true);}finally{$('cancel').disabled=false;}}});
form.addEventListener('input',changed);$('foods').addEventListener('input',changed);$('premix-json').addEventListener('input',changed);
form.addEventListener('submit',async e=>{
  e.preventDefault();if(loading||activeJob)return;
  const token=revision;$('download-plan').disabled=true;$('download-coupled').disabled=true;$('result').hidden=true;$('planning').hidden=true;$('coupled').hidden=true;$('diagnostic').hidden=true;
  try{
    if(coupledMode()){
      const args=coupledScenario();setBusy(true);message('Preparando a simulação RuFaS com o otimizador acoplado…');
      const job=await request('/api/acoplado/jobs',args);activeJob=job.job_id;
      renderCoupled(await poll(job.job_id,token));dirty=false;
      message('Simulação RuFaS concluída com a dieta do otimizador em cada reformulação. Consulte as hipóteses do modelo.');
    }else{
      const args=scenario();setBusy(true);message('Preparando cálculo com o motor RuFaS…');
      const job=await request('/api/rufas/jobs',args);activeJob=job.job_id;
      renderPlan(await poll(job.job_id,token));dirty=false;
      message('Dieta calculada com RuFaS nativo e restrições conferidas. Consulte as hipóteses do modelo.');
    }
  }catch(e){message(e.message,true);}finally{activeJob=null;setBusy(false);}
});
function download(data,name){const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
$('download-plan').addEventListener('click',()=>{if(!dirty&&plan)download(plan,'planejamento-rufas-'+plan.padrao.toLowerCase()+'.json');});
$('download-coupled').addEventListener('click',()=>{if(!dirty&&coupled)download(coupled,'simulacao-acoplada-rufas-'+coupled.padrao.toLowerCase()+'.json');});
function syncOptimizer(){
  const nrc=$('standard').value==='NRC',pulp=$('optimizer').querySelector('[value=pulp]');
  pulp.disabled=nrc;if(nrc)$('optimizer').value='slsqp';
}
function applySimMode(){
  const on=coupledMode();document.body.classList.toggle('coupled',on);
  form.elements.dias_simulacao.max=on?730:304;form.elements.intervalo_dieta.max=on?60:30;
  $('solve').textContent=on?'Rodar simulação com otimizador':'Calcular dieta';
}
$('sim-mode').addEventListener('change',()=>{applySimMode();changed();});
function applyProfile(){
  if(!profile)return;
  Object.entries({...profile.parametros,...profile.hipoteses}).forEach(([k,v])=>{if(form.elements[k])form.elements[k].value=v;});
  form.elements.data_inicio.value=new Date().toLocaleDateString('sv-SE');
  $('animal-profile').textContent=profile.nome+' · HO · animal '+profile.animal_id+' · fonte: '+profile.fonte;
}
$('reset').addEventListener('click',()=>{form.reset();applyProfile();applySimMode();syncOptimizer();setMode();});
async function loadFoods(){
  const r=await fetch('/api/rufas/alimentos?padrao='+encodeURIComponent($('standard').value));if(!r.ok)throw Error('Biblioteca RuFaS indisponível');
  const data=await r.json();$('foods').replaceChildren();
  data.alimentos.forEach(f=>{
    const use=document.createElement('input');use.type='checkbox';use.checked=true;use.setAttribute('aria-label','Usar '+f.nome);
    const price=document.createElement('input');Object.assign(price,{type:'text',inputMode:'decimal',value:String(f.preco_natural),required:true,className:'price'});price.setAttribute('aria-label','Preço de '+f.nome);
    const stock=document.createElement('input');Object.assign(stock,{type:'text',inputMode:'decimal',placeholder:'Sem limite',className:'stock'});stock.setAttribute('aria-label','Estoque kg MS de '+f.nome);
    const tr=row($('foods'),[use,f.nome,fmt(f.MS,1),price,stock]);tr.dataset.id=f.id;tr.dataset.name=f.nome;tr.lastElementChild.classList.add('only-plan');
    const hint=document.createElement('small');hint.className='stock-help';tr.lastElementChild.append(hint);
  });updateStockHints();
  $('premix').textContent='Premix fixo: '+fmt(data.premix.kg_ms)+' kg/vaca/dia a '+money(data.premix.preco)+'/kg. Ca/P somente se declarados. '+(data.ausentes.length?'Sem correspondência neste padrão: '+data.ausentes.join(', ')+'.':'');
}
async function setMode(){
  loading=true;setBusy(true);changed();
  try{await loadFoods();$('connection').textContent='RuFaS nativo · NRC / NASEM · local';message('');}
  catch(e){message(e.message,true);}finally{loading=false;setBusy(false);}
}
$('standard').addEventListener('change',()=>{syncOptimizer();setMode();});
(async()=>{
  try{const r=await fetch('/api/rufas/perfil');if(!r.ok)throw Error('Perfil RuFaS indisponível');profile=await r.json();applyProfile();applySimMode();syncOptimizer();await setMode();}
  catch(e){message(e.message,true);$('solve').disabled=true;}
})();
