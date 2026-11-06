const $=id=>document.getElementById(id);
const defs={strategy1:["Strategy 1","EMA + Confirmed Trailing Buy","📈"],strategy2:["Strategy 2","Not implemented in this build","🎯"],strategy3:["Strategy 3","Not implemented in this build","📊"]};
async function api(path,body){const r=await fetch(path,{method:body?"POST":"GET",headers:body?{"Content-Type":"application/json"}:{},body:body?JSON.stringify(body):undefined});const d=await r.json();if(!r.ok)throw Error(d.error||"Request failed");return d}
function status(s){return s.emergency_stop?"EMERGENCY STOP":s.running?s.message?.toLowerCase().includes("not authorized")?"RUNNING · BUY LOCKED":"RUNNING · REAL ORDERS ARMED":"STOPPED"}

function updateStatus(d){
  for(const [key,s] of Object.entries(d.strategies)){
    const dot=$(key+"-dot");
    const label=$(key+"-status");
    if(dot) dot.classList.toggle("running-dot",!!s.running);
    if(label && key==="strategy1") label.textContent=status(s);
  }
  $("riskStatus").textContent=d.risk_latched?`LOCKED: ${d.risk_message}`:d.risk_message;
  $("homeStatuses").innerHTML=`<div class="line"><span>Strategy 1</span><strong>${status(d.strategies.strategy1)}</strong></div><div class="line"><span>Global risk lock</span><strong>${d.risk_latched?"ACTIVE":"Not triggered / not live-monitored"}</strong></div>`;
}

async function init(){if(!$('strategyList'))return;document.querySelectorAll('[data-home]').forEach(b=>b.onclick=()=>location.href='/');try{const d=await api('/api/status');const g=d.parameters.global;$('globalSettingsForm').elements.daily_profit_cap_pct.value=g.daily_profit_cap_pct;$('globalSettingsForm').elements.peak_drawdown_limit_pct.value=g.peak_drawdown_limit_pct;$('riskStatus').textContent=d.risk_latched?`LOCKED: ${d.risk_message}`:d.risk_message;$('todayPnl').textContent=d.today_total_pnl==null?'Not connected':d.today_total_pnl; $('strategyList').innerHTML=Object.entries(defs).map(([k,v])=>`<button class="strategy-card" ${k==='strategy1'?'data-open="strategy1"':'disabled'}><span class="strategy-icon">${v[2]}</span><span class="strategy-content"><span class="strategy-title"><span id="${k}-dot" class="status-dot"></span>${v[0]}</span><div class="strategy-desc">${v[1]} · <span id="${k}-status">${k==='strategy1'?status(d.strategies[k]):'Coming later'}</span></div></span><span class="arrow">${k==='strategy1'?'›':'·'}</span></button>`).join('');document.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>location.href='/'+b.dataset.open);$('homeStatuses').innerHTML=`<div class="line"><span>Strategy 1</span><strong>${status(d.strategies.strategy1)}</strong></div><div class="line"><span>Global risk lock</span><strong>${d.risk_latched?'ACTIVE':'Not triggered / not live-monitored'}</strong></div>`;}catch(e){$('riskStatus').textContent=e.message;$('strategyList').textContent=e.message}}
if($('globalSettingsForm'))$('globalSettingsForm').onsubmit=async e=>{e.preventDefault();const p=Object.fromEntries(new FormData(e.target));for(const k of Object.keys(p))p[k]=Number(p[k]);try{const d=await api('/api/global/settings',p);$('globalSettingsMessage').textContent='Saved. This does not clear a triggered risk lock.';$('riskStatus').textContent=d.risk_latched?`LOCKED: ${d.risk_message}`:d.risk_message;}catch(err){$('globalSettingsMessage').textContent=err.message}};
init();
setInterval(async () => {
  if (!$("strategyList")) return;
  try {
    const d = await api("/api/status");
    updateStatus(d);
  } catch(error) {
    $("riskStatus").textContent = error.message;
  }
}, 3000);
