'use strict';
const CC = ['USD','EUR','EGP','RUB'];
const formData = form => Object.fromEntries(new FormData(form));
const table = (headers, rows) => `<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
const tr = cells => `<tr>${cells.map(c=>`<td>${c}</td>`).join('')}</tr>`;
const numberField = (key, value='', extra='') => field(key, `<input type="number" name="${key}" value="${esc(value)}" ${/step=/.test(extra)?'':'step=".01"'} ${extra}>`);
const openShift = () => state.shifts.find(s=>!s.closed);
function unitPrice(line) { return state.products.find(p=>p.id===line.id)?.prices[payment.currency] ?? line.prices[payment.currency]; }
async function refresh() {
  state = await api('state');
  sales = await api('sales');
  template = state.options.template;
  cart = cart.map(l=>({...state.products.find(p=>p.id===l.id),qty:l.qty,discount:l.discount})).filter(l=>l.id);
}
function exportCSV(name, rows) {
  const csv = '\uFEFF'+rows.map(row=>row.map(cell=>{
    let value=String(cell??'');
    if (/^[=+@\-]/.test(value) && typeof cell==='string') value="'"+value;
    return '"'+value.replaceAll('"','""')+'"';
  }).join(',')).join('\r\n');
  const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));
  const a=document.createElement('a'); a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function modal(title, content) {
  let dialog=$('operationDialog');
  if (!dialog) { dialog=document.createElement('dialog');dialog.id='operationDialog';document.body.append(dialog); }
  dialog.innerHTML=`<h2>${esc(title)}</h2>${content}<div class="actions"><button id="dismiss" class="secondary">${t('close')}</button></div>`;
  $('dismiss').onclick=()=>dialog.close();dialog.showModal();return dialog;
}

const inventoryBase=renderInventory;
renderInventory=function() {
  inventoryBase();
  $('productForm').querySelector('.fields').insertAdjacentHTML('beforeend',numberField('minStock',0,'min="0" step=".001"').replace('name="minStock"','name="min_stock"'));
  $('batchForm').querySelector('.fields').insertAdjacentHTML('afterend',`<div class="fields">${field('supplier','<input name="supplier" list="suppliers" dir="auto">')}${field('invoice','<input name="invoice" dir="auto">')}</div><datalist id="suppliers">${[...new Set(state.batches.map(b=>b.supplier).filter(Boolean))].map(s=>`<option value="${esc(s)}">`).join('')}</datalist>`);
  const value=state.products.reduce((n,p)=>n+p.stock_value,0);
  const low=state.products.filter(p=>p.stock<=p.min_stock);
  $('content').querySelector('.stats').insertAdjacentHTML('beforeend',`<div class="stat"><small>${t('warehouseValue')}</small><strong>${fmt(value)}</strong></div>`);
  const card=document.createElement('section');card.className='card';card.innerHTML=`<h2>${t('reorder')} <span class="badge">${low.length}</span></h2>${table([t('product'),t('stock'),t('minStock')],low.map(p=>tr([`<bdi>${esc(p.name)}</bdi>`,`${p.stock} ${t(p.unit)}`,p.min_stock])))}<button id="reorderExport" class="secondary">${t('download')}</button>`;
  $('content').querySelector('.stats').after(card);
  $('reorderExport').onclick=()=>exportCSV('reorder.csv',[['Name','Barcode','Stock','Minimum','Unit'],...low.map(p=>[p.name,p.barcode,p.stock,p.min_stock,p.unit])]);
  // Supplier information is retained separately on every purchase batch.
  const batchTable=$('content').querySelector('.card:last-child table');
  batchTable.querySelector('thead tr').insertAdjacentHTML('beforeend',`<th>${t('supplier')} / ${t('invoice')}</th>`);
  batchTable.querySelectorAll('tbody tr').forEach((row,i)=>row.insertAdjacentHTML('beforeend',`<td><bdi>${esc(state.batches[i].supplier)}</bdi><small>${esc(state.batches[i].invoice)}</small></td>`));
};

function renderAdjustments() {
  $('content').innerHTML=`<div class="grid"><form id="adjustForm" class="card"><h2>${t('adjustments')}</h2>${field('product',`<select name="product">${productOptions()}</select>`)}<p id="currentStock" class="muted"></p>${field('movement',`<select name="kind">${opt('count',t('count'),'count')}${opt('writeoff',t('writeoff'),'count')}</select>`)}${numberField('observed','','required min="0" step=".001"').replace('name="observed"','name="qty"')}${numberField('cost','','min="0" step=".0001"')}${field('reason','<input name="reason" required dir="auto">')}<p class="muted">${t('adjustHint')}</p><button ${!state.products.length?'disabled':''}>${t('save')}</button></form><div class="card"><h2>${t('warehouseValue')}</h2><div class="total">${fmt(state.products.reduce((n,p)=>n+p.stock_value,0))}</div><p class="muted">${t('fifo')}</p></div></div><section class="card"><h2>${t('journal')}</h2>${table([t('date'),t('product'),t('movement'),t('qty'),t('reason')],state.movements.map(m=>tr([new Date(m.created).toLocaleString(lang),`<bdi>${esc(m.name)}</bdi>`,t(m.kind),m.qty/1000,esc(m.reason)])))}</section>`;
  const f=$('adjustForm');
  f.elements.product.onchange=()=>{const p=state.products.find(p=>p.id===f.elements.product.value);$('currentStock').textContent=p?`${t('currentStock')}: ${p.stock} ${t(p.unit)}`:'';};
  f.elements.product.onchange();
  f.onsubmit=e=>{e.preventDefault();action(async()=>{const data=formData(f),p=state.products.find(p=>p.id===data.product);await api('adjust',{...data,expected_stock:p.stock});await refresh();notice(t('saved'));renderAdjustments();});};
}

function shiftTable(shift) {
  const summary=shift.summary;
  return table([t('currency'),t('opening'),t('expected'),t('actual'),t('difference'),t('cash'),t('card'),t('transfer'),t('fees')],CC.map(c=>tr([c,fmt(shift.opening[c]),fmt(summary.expected[c]),shift.closing?fmt(shift.closing[c]):'—',summary.difference?fmt(summary.difference[c]):'—',...['cash','card','transfer'].map(m=>fmt(summary.payments[c][m])),fmt(summary.fees[c])])));
}
function cashFields(key, value) {
  return `<div class="fields">${CC.map(c=>`<label>${c}<input name="${c}" type="number" min="0" step=".01" required ${value===undefined?'':`value="${value}"`} aria-label="${t(key)} ${c}"></label>`).join('')}</div>`;
}
function renderShifts() {
  const shift=openShift();
  $('content').innerHTML=shift?`<section class="card"><h2>${t('shift')} #${shift.id} · ${t('active')}</h2><p class="muted">${new Date(shift.opened).toLocaleString(lang)}</p>${shiftTable(shift)}</section><div class="grid"><form class="card" id="closeShift"><h2>${t('closeShift')}</h2><p class="muted">${t('checkCash')}</p>${cashFields('actual')}<div id="closePreview"></div><button>${t('closeShift')}</button></form><form class="card" id="cashMovement"><h2>${t('cashMove')}</h2>${field('currency',`<select name="currency">${currencies('EGP')}</select>`)}${numberField('amount','','required')}${field('reason','<input name="reason" required dir="auto">')}<button>${t('save')}</button><ul class="muted">${shift.summary.movements.map(m=>`<li>${esc(m.reason)}: ${fmt(m.amount)} ${m.currency}</li>`).join('')}</ul></form></div>`:`<form class="card" id="openShift"><h2>${t('openShift')}</h2>${cashFields('opening',0)}<button>${t('openShift')}</button></form>`;
  $('content').insertAdjacentHTML('beforeend',`<section class="card"><h2>${t('shifts')}</h2>${state.shifts.filter(s=>s.closed).map(s=>`<details><summary>#${s.id} · ${new Date(s.opened).toLocaleString(lang)} → ${new Date(s.closed).toLocaleString(lang)}</summary>${shiftTable(s)}<button class="secondary" data-shift-export="${s.id}">${t('download')}</button></details>`).join('')||`<p class="muted">${t('empty')}</p>`}</section>`);
  if ($('openShift')) $('openShift').onsubmit=e=>{e.preventDefault();action(async()=>{await api('shift-open',{opening:formData(e.target)});await refresh();renderShifts();notice(t('saved'));});};
  if ($('closeShift')) {
    $('closeShift').oninput=()=>{
      const data=formData($('closeShift'));
      $('closePreview').innerHTML=`<p class="muted">${t('difference')}: ${CC.map(c=>`${c} ${data[c]===''?'—':fmt(Number(data[c])-shift.summary.expected[c])}`).join(' / ')}</p>`;
    };
    $('closeShift').onsubmit=e=>{e.preventDefault();action(async()=>{await api('shift-close',{id:shift.id,closing:formData(e.target)});await refresh();renderShifts();notice(t('saved'));if(state.backup_warning)notice(t('copyWarning')+state.backup_warning,true);});};
    $('cashMovement').onsubmit=e=>{e.preventDefault();action(async()=>{await api('cash-movement',formData(e.target));await refresh();renderShifts();notice(t('saved'));});};
  }
  $('content').querySelectorAll('[data-shift-export]').forEach(b=>b.onclick=()=>{
    const s=state.shifts.find(s=>s.id===Number(b.dataset.shiftExport));
    exportCSV(`shift-${s.id}.csv`,[['Currency','Opening','Expected','Actual','Difference','Cash sales','Card','Transfer','Fees'],...CC.map(c=>[c,s.opening[c],s.summary.expected[c],s.closing[c],s.summary.difference[c],s.summary.payments[c].cash,s.summary.payments[c].card,s.summary.payments[c].transfer,s.summary.fees[c]])]);
  });
}

function reportEvents() {
  const result=[];
  for (const sale of sales) {
    result.push({...sale,event_id:sale.id,sign:1,source:sale});
    if (sale.void) result.push({...sale,event_id:sale.id+' / '+t('negative'),created:sale.void.created,sign:-1,fee:sale.void.fee_refunded?(sale.fee||0):0,source:sale,reversal:true});
  }
  return result.sort((a,b)=>b.created.localeCompare(a.created));
}
filteredSales=function() {
  return reportEvents().filter(s=>{
    const d=new Date(s.created), local=[d.getFullYear(),String(d.getMonth()+1).padStart(2,'0'),String(d.getDate()).padStart(2,'0')].join('-');
    return (!filters.from||local>=filters.from)&&(!filters.to||local<=filters.to)&&(!filters.currency||s.currency===filters.currency)&&(!filters.method||s.method===filters.method);
  });
};
converted=function(s) {
  const factor=s.rates[filters.display]/s.rates[s.currency],sign=s.sign??1;
  const revenue=sign*s.total*factor,cost=sign*s.cost_egp/s.rates.EGP*s.rates[filters.display],fee=sign*(s.fee||0)*factor;
  return {revenue,cost,profit:revenue-cost,fee,afterFees:revenue-cost-fee};
};
renderReports=function() {
  const selected=filteredSales(),summary=selected.reduce((a,s)=>{const c=converted(s);for(const k in a)a[k]+=c[k];return a;},{revenue:0,cost:0,profit:0,fee:0,afterFees:0});
  $('content').innerHTML=`<section class="card"><h2>${t('summary')}</h2><div class="fields">${field('from',`<input type="date" id="from" value="${filters.from}">`)}${field('to',`<input type="date" id="to" value="${filters.to}">`)}${field('currency',`<select id="filterCurrency">${currencies(filters.currency,true)}</select>`)}${field('method',`<select id="filterMethod">${opt('',t('all'),filters.method)}${['cash','card','transfer'].map(m=>opt(m,t(m),filters.method)).join('')}</select>`)}${field('display',`<select id="display">${currencies(filters.display)}</select>`)}</div><p class="muted">${t('cancelHint')}</p></section><div class="stats">${[['revenue','revenue'],['cost','costTotal'],['profit','profit'],['fee','fees'],['afterFees','afterFees']].map(([k,label])=>`<div class="stat"><small>${t(label)} · ${filters.display}</small><strong>${fmt(summary[k])}</strong></div>`).join('')}</div><section class="card"><div class="actions"><h2>${t('sales')}</h2><button id="export" class="secondary">${t('download')}</button></div>${table([t('date'),t('payment'),t('total'),t('profit')+' · '+filters.display,t('fees'),t('afterFees'),t('status')],selected.map(s=>{const c=converted(s);return tr([`${new Date(s.created).toLocaleString(lang)}<small>${esc(s.event_id)}</small>`,`${t(s.method)} / ${s.currency}`,`${fmt(s.sign*s.total)} ${s.currency}`,fmt(c.profit),fmt(c.fee),fmt(c.afterFees),s.reversal?`${t('negative')}: ${esc(s.void.reason)}`:`${s.void?`<span class="badge">${t('voided')}</span>`:''}<button class="secondary" data-receipt="${s.id}">${t('printHistory')}</button> ${!s.void?`<button class="secondary" data-void="${s.id}">${t('voidSale')}</button>`:''}`]);}))}${!selected.length?`<p class="empty">${t('noResults')}</p>`:''}</section>`;
  for(const [id,key] of [['from','from'],['to','to'],['filterCurrency','currency'],['filterMethod','method'],['display','display']])$(id).onchange=e=>{filters[key]=e.target.value;renderReports();};
  $('export').onclick=()=>exportCSV('sales.csv',[['ID','Date','Payment currency','Method','Amount','Report currency','Revenue','Cost','Gross profit','Fees','Profit after fees','Correction reason'],...selected.map(s=>{const c=converted(s);return [s.event_id,s.created,s.currency,s.method,s.sign*s.total,filters.display,...['revenue','cost','profit','fee','afterFees'].map(k=>round(c[k])),s.reversal?s.void.reason:''];})]);
  $('content').querySelectorAll('[data-receipt]').forEach(b=>b.onclick=()=>printReceipt(sales.find(s=>s.id===b.dataset.receipt)));
  $('content').querySelectorAll('[data-void]').forEach(b=>b.onclick=()=>{
    if(!openShift()){notice(t('shiftRequired'),true);return;}
    const s=sales.find(s=>s.id===b.dataset.void);
    const dialog=modal(t('voidSale')+' · '+s.id,`<p>${fmt(s.total)} ${s.currency} · ${t(s.method)}</p><p class="muted">${t('cancelHint')}</p><form id="voidForm">${field('voidReason','<input name="reason" required dir="auto">')}<label class="check"><input name="confirmed" type="checkbox" required>${t('reverseConfirmed')}</label>${s.fee?`<label class="check"><input name="fee_refunded" type="checkbox">${t('feeRefunded')}</label><p class="muted">${t('cancelledFeeHint')}</p>`:''}<button>${t('confirm')}</button></form>`);
    $('voidForm').onsubmit=e=>{e.preventDefault();action(async()=>{const f=e.target;await api('void',{id:s.id,reason:f.elements.reason.value,confirmed:f.elements.confirmed.checked,fee_refunded:!!f.elements.fee_refunded?.checked});dialog.close();await refresh();renderReports();notice(t('saved'));});};
  });
};

let labelJob=[];
function labelMarkup(p) {
  return `<div class="label-preview" style="width:${template.width}mm;height:${template.height}mm;padding:1.5mm"><bdi class="name">${esc(p.name)}</bdi><div class="prices" dir="ltr">${CC.map(c=>`<span>${c} ${fmt(p.prices[c])}</span>`).join('')}</div>${barcodeSVG(p.barcode)}<span class="code">${esc(p.barcode)}</span></div>`;
}
const printBase=showPrint;
showPrint=function(html,width,height) {
  printBase(html,width,height);
  const pageHeight=height||Math.ceil($('printArea').scrollHeight*25.4/96)+8;
  $('pageStyle').textContent=`@media print{@page{size:${width}mm ${pageHeight}mm;margin:${height?'0':'3mm'}}`;
  const button=$('labelsPrinted');if(button)button.hidden=true;
};
function printLabelJob(items) {
  if(!state.configured)throw Error(t('ratesRequired'));
  if(!items.length)throw Error(t('noSelection'));
  const count=items.reduce((n,i)=>n+i.copies,0);
  if(count>1000)throw Error(t('maxLabels'));
  labelJob=items.map(i=>({id:i.p.id,signature:i.p.signature}));
  if(items.some(i=>i.p.barcode.length>10)&&template.width<50)notice(t('enlarge'),true);
  showPrint(items.map(i=>Array.from({length:i.copies},()=>labelMarkup(i.p)).join('')).join(''),template.width,template.height);
  let button=$('labelsPrinted');
  if(!button){button=document.createElement('button');button.id='labelsPrinted';$('printDialog').querySelector('.actions').append(button);}
  button.hidden=false;button.textContent=t('printed');
  button.onclick=()=>action(async()=>{await api('labels-confirm',{items:labelJob});$('printDialog').close();await refresh();if(page==='labelsBatch')renderLabels();notice(t('saved'));});
}
printLabel=function(p){printLabelJob([{p,copies:1}]);};
let changedOnly=true;
function renderLabels() {
  const products=state.products.filter(p=>!changedOnly||p.label_changed);
  $('content').innerHTML=`<section class="card"><h2>${t('labelsBatch')}</h2><p class="muted">${t('labelHint')} ${t('labelHint2')}</p><label class="check"><input id="changedOnly" type="checkbox" ${changedOnly?'checked':''}>${t('changedOnly')}</label><div class="actions"><button id="selectLabels" class="secondary">${t('selectAll')}</button><button id="stockCopies" class="secondary">${t('stockCopies')}</button></div>${table([t('product'),t('stock'),t('status'),t('copies')],products.map(p=>tr([`<bdi>${esc(p.name)}</bdi>`,p.stock,p.label_changed?t('needed'):'—',`<input type="number" min="0" max="1000" step="1" value="0" data-copies="${p.id}" aria-label="${t('copies')} ${esc(p.name)}">`])))}<button id="printLabels">${t('printSelected')}</button></section>`;
  $('changedOnly').onchange=e=>{changedOnly=e.target.checked;renderLabels();};
  $('selectLabels').onclick=()=>$('content').querySelectorAll('[data-copies]').forEach(e=>e.value=1);
  $('stockCopies').onclick=()=>$('content').querySelectorAll('[data-copies]').forEach(e=>{const p=products.find(p=>p.id===e.dataset.copies);e.value=p.unit==='piece'?Math.min(1000,p.stock):(p.stock>0?1:0);});
  $('printLabels').onclick=()=>action(async()=>{
    const items=[...$('content').querySelectorAll('[data-copies]')].map(e=>({p:products.find(p=>p.id===e.dataset.copies),copies:Number(e.value)}));
    if(items.some(i=>!Number.isInteger(i.copies)||i.copies<0))throw Error(t('invalid'));
    printLabelJob(items.filter(i=>i.copies>0));
  });
}

async function previewChanges(data) {
  const preview=await api('preview',data);
  const dialog=modal(t('preview'),`<p class="muted">${t('roundHint')}</p>${table([t('product'),t('oldPrices'),t('newPrices')],preview.products.map(p=>tr([`<bdi>${esc(p.name)}</bdi>`,CC.map(c=>`${c} ${fmt(p.old[c])}`).join('<br>'),CC.map(c=>`${c} ${fmt(p.new[c])}`).join('<br>')])))}<button id="applyChanges">${t('apply')}</button>`);
  $('applyChanges').onclick=()=>action(async()=>{await api('reprice',{...data,revision:preview.revision});dialog.close();await refresh();renderSettings();notice(t('saved'));});
}
renderSettings=function() {
  const options=state.options;
  $('content').innerHTML=`<div class="grid"><form id="ratesForm" class="card"><h2>${t('rates')}</h2><p class="muted">${t('rateHint')}</p><div class="fields">${CC.filter(c=>c!=='USD').map(c=>`<label>1 USD → ${c}<input name="${c}" type="number" min=".000001" step="any" required value="${state.rates[c]}"></label>`).join('')}</div><button>${t('preview')}</button></form><form id="roundingForm" class="card"><h2>${t('rounding')}</h2><p class="muted">${t('roundHint')}</p><div class="fields">${CC.map(c=>`<label>${c}<input type="number" name="${c}" min=".01" max="100" step=".01" value="${options.rounding[c]}" ${c==='USD'?'readonly':''} required></label>`).join('')}</div><button>${t('preview')}</button></form></div><div class="grid"><form id="feesForm" class="card"><h2>${t('fees')}</h2><p class="muted">${t('feeHint')}</p>${['card','transfer'].map(m=>`<h3>${t(m)}</h3><div class="fields">${field('percent',`<input name="${m}_percent" type="number" min="0" max="100" step=".01" value="${options.fees[m].percent}" required>`)}${field('fixedFee',`<input name="${m}_fixed" type="number" min="0" step=".01" value="${options.fees[m].fixed}" required>`)}</div>`).join('')}<button>${t('save')}</button></form><form id="templateForm" class="card"><h2>${t('labels')}</h2>${field('shopName',`<input name="shop_name" value="${esc(options.shop_name)}" required dir="auto">`)}<div class="fields">${['width','height','receiptWidth'].map(k=>field(k,`<input name="${k}" type="number" min="10" max="200" step="any" required value="${template[k]}">`)).join('')}</div><p class="muted">${t('labelHint')}</p><button>${t('save')}</button></form></div><section class="card"><h2>${t('backup')}</h2><form id="backupFolderForm">${field('backupFolder',`<input name="backup_dir" value="${esc(options.backup_dir)}" placeholder="/Volumes/Backup/Nile" dir="ltr">`)}<p class="muted">${t('backupHint')}</p>${state.can_choose_folder?`<button type="button" id="chooseBackup" class="secondary">${t('chooseFolder')}</button>`:''}<button>${t('save')}</button></form><div class="actions"><a class="button secondary" href="/api/backup-download" download>${t('backupDownload')}</a></div><p class="muted">${t('backupTest')}</p>${state.backup_warning?`<p class="error">${t('copyWarning')}${esc(state.backup_warning)}</p>`:''}</section><form class="card" id="restoreForm"><h2>${t('restore')}</h2><p class="muted">${t('restoreHint')}</p>${field('restoreSelect','<input type="file" id="restoreFile" accept=".sqlite,.db" required>')}<input id="restoreConfirm" required pattern="RESTORE" placeholder="RESTORE" aria-label="RESTORE"><div class="actions"><button ${openShift()?'disabled':''}>${t('restore')}</button></div></form><section class="card"><h2>Nile Store · ${t('version')} ${esc(state.version)}</h2><p class="muted">${t('updateHint')}</p><p>${t('dataFolder')}: <bdi>${esc(state.data_dir)}</bdi></p></section>`;
  $('ratesForm').onsubmit=e=>{e.preventDefault();action(()=>previewChanges({rates:formData(e.target)}));};
  $('roundingForm').onsubmit=e=>{e.preventDefault();action(()=>previewChanges({options:{rounding:formData(e.target)}}));};
  $('feesForm').onsubmit=e=>{e.preventDefault();action(async()=>{const data=formData(e.target),fees={};for(const m of ['card','transfer'])fees[m]={percent:data[m+'_percent'],fixed:data[m+'_fixed']};await api('options',{fees});await refresh();renderSettings();notice(t('saved'));});};
  $('templateForm').onsubmit=e=>{e.preventDefault();action(async()=>{const data=formData(e.target);await api('options',{shop_name:data.shop_name,template:Object.fromEntries(['width','height','receiptWidth'].map(k=>[k,data[k]]))});await refresh();renderSettings();notice(t('saved'));});};
  if($('chooseBackup'))$('chooseBackup').onclick=()=>action(async()=>{const r=await api('choose-backup-folder',{});if(r.folder)$('backupFolderForm').elements.backup_dir.value=r.folder;});
  $('backupFolderForm').onsubmit=e=>{e.preventDefault();action(async()=>{await api('options',formData(e.target));const result=await api('backup',{});await refresh();renderSettings();notice(t('backupSaved')+result.file);if(result.warning)notice(t('copyWarning')+result.warning,true);});};
  $('restoreForm').onsubmit=e=>{e.preventDefault();action(async()=>{
    const file=$('restoreFile').files[0];if(!file||file.size>64*1024*1024)throw Error(t('invalid'));
    const encoded=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(Error(t('invalid')));reader.readAsDataURL(file);});
    const result=await api('restore',{file:encoded,confirmed:$('restoreConfirm').value});cart=[];lastSale=null;await refresh();renderSettings();notice(t('restoreDone')+result.safety_copy);
  });};
};
$('backup').onclick=()=>action(async()=>{const result=await api('backup',{});notice(t('backupSaved')+result.file+(result.external_file?' / '+result.external_file:''));if(result.warning)notice(t('copyWarning')+result.warning,true);});
action(async()=>{
  await refresh();
  // Preserve a first-version custom print layout when upgrading this browser.
  if(localStorage.getItem('nile-template')&&!localStorage.getItem('nile-template-migrated')){
    const saved=JSON.parse(localStorage.getItem('nile-template'));
    await api('options',{template:saved});localStorage.setItem('nile-template-migrated','1');await refresh();
  }
  render();if(state.backup_warning)notice(t('copyWarning')+state.backup_warning,true);
});
