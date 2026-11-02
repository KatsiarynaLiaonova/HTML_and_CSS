'use strict';
(() => {
  const labels={
    ru:{login:'Вход в ALI BABA',username:'Логин',password:'Пароль',signIn:'Войти',signOut:'Выйти',accounts:'Кассиры',addCashier:'Добавить кассира',register:'Касса',passwordHint:'Не менее 10 символов. Пароль передайте кассиру лично.',created:'Учетная запись создана',owner:'Владелец',cashier:'Кассир'},
    en:{login:'Sign in to ALI BABA',username:'Login',password:'Password',signIn:'Sign in',signOut:'Sign out',accounts:'Cashiers',addCashier:'Add cashier',register:'Register',passwordHint:'At least 10 characters. Give the password to the cashier privately.',created:'Account created',owner:'Owner',cashier:'Cashier'},
    ar:{login:'تسجيل الدخول إلى ALI BABA',username:'اسم الدخول',password:'كلمة المرور',signIn:'دخول',signOut:'خروج',accounts:'الكاشير',addCashier:'إضافة كاشير',register:'نقطة البيع',passwordHint:'10 أحرف على الأقل. سلّم كلمة المرور للكاشير بشكل خاص.',created:'تم إنشاء الحساب',owner:'المالك',cashier:'كاشير'}
  };
  const text=key=>(labels[lang]||labels.ru)[key];
  window.aliBabaSession=null;
  window.aliBabaAuthEpoch=0;
  let initialLanding=true;
  window.aliBabaShowLogin=()=>{
    if(document.querySelector('#loginDialog[open]'))return;
    window.aliBabaAuthEpoch++;window.aliBabaSession=null;
    cart=[];lastSale=null;sales=[];
    $('content').innerHTML='';$('nav').innerHTML='';$('backup').hidden=true;if($('accountBar'))$('accountBar').remove();
    const previous=$('loginDialog');if(previous)previous.remove();
    const dialog=document.createElement('dialog');dialog.id='loginDialog';
    dialog.innerHTML=`<h2>${text('login')}</h2><form id="loginForm"><label>${text('username')}<input name="login" required autocomplete="username" maxlength="80"></label><label>${text('password')}<input name="password" type="password" required autocomplete="current-password" maxlength="256"></label><p id="loginError" class="error" hidden role="alert"></p><button type="submit">${text('signIn')}</button></form>`;
    dialog.addEventListener('cancel',event=>event.preventDefault());document.body.append(dialog);dialog.showModal();
    const form=$('loginForm');
    form.onsubmit=async event=>{
      event.preventDefault();const button=form.querySelector('button');button.disabled=true;$('loginError').hidden=true;
      try{
        const response=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({login:form.elements.login.value,password:form.elements.password.value})});
        const result=await response.json();if(!response.ok)throw Error(result.error);
        window.aliBabaAuthEpoch++;window.aliBabaSession={...result,shared:true};form.elements.password.value='';
        await refresh();page=result.account.role==='owner'?'reports':'pos';dialog.close();dialog.remove();render();
      }catch(error){$('loginError').textContent=error.message;$('loginError').hidden=false;}
      finally{button.disabled=false;}
    };
    form.elements.login.focus();
  };
  const oldRender=render;
  render=function(){
    const account=state?.account;
    if(initialLanding&&state?.shared&&account?.role==='owner')page='reports';
    if(account)initialLanding=false;
    if(account?.role==='cashier'&&page==='adjustments')page='pos';
    oldRender();
    let connection=$('connectionSetup');
    if(!connection){connection=document.createElement('button');connection.id='connectionSetup';connection.type='button';connection.className='secondary';connection.textContent='Подключение / Connection';document.querySelector('header').append(connection);connection.onclick=()=>action(async()=>{if(!window.pywebview?.api?.configure_connection)throw Error('Откройте настройки подключения в установленном приложении ALI BABA');await window.pywebview.api.configure_connection();});}
    connection.hidden=account?.role==='cashier'||!window.pywebview?.api?.configure_connection;
    $('backup').hidden=account?.role==='cashier';
    if(account?.role==='cashier')for(const button of document.querySelectorAll('[data-page=adjustments],[data-edit]'))button.hidden=true;
    let bar=$('accountBar');
    if(!state?.shared){if(bar)bar.remove();return;}
    if(!bar){bar=document.createElement('div');bar.id='accountBar';bar.className='actions';document.querySelector('header').after(bar);}
    const register=state.registers.find(item=>item.id===state.register_id);
    bar.innerHTML=`<span><b>${esc(account.login)}</b> · ${text(account.role)} · ${esc(register?.name||'')}</span><button type="button" class="secondary" id="refreshShared">Обновить / Refresh</button><button type="button" class="secondary" id="logout">${text('signOut')}</button>`;
    $('refreshShared').onclick=()=>action(async()=>{await refresh();render();});
    $('logout').onclick=()=>action(async()=>{if(busy)throw Error('Дождитесь завершения продажи / Wait for the sale to finish');await api('logout',{});window.aliBabaShowLogin();});
  };
  const oldPOS=renderPOS;
  renderPOS=function(){
    oldPOS();const pending=unresolvedCheckout();if(!pending)return;
    for(const field of document.querySelectorAll('#content input,#content select,#checkout,[data-remove],[data-match]'))field.disabled=true;
    $('content').insertAdjacentHTML('afterbegin','<div class="error" role="alert">Результат предыдущей продажи неизвестен / Previous sale result is unknown.<br><button type="button" id="recoverCheckout">Проверить предыдущую продажу / Recover previous sale</button></div>');
    $('recoverCheckout').onclick=()=>action(async()=>{const button=$('recoverCheckout');button.disabled=true;busy=true;try{lastSale=await api('checkout',pending.data);cart=[];payment.value=0;payment.tender='';await refresh();renderPOS();notice(t('newSale')+' · '+lastSale.id);}finally{busy=false;if($('recoverCheckout'))$('recoverCheckout').disabled=false;}});
  };
  const oldInventory=renderInventory;
  renderInventory=function(){oldInventory();if(state?.account?.role==='cashier')for(const button of document.querySelectorAll('[data-edit]'))button.hidden=true;};
  const beforeRegisterFilter=filteredSales;
  filteredSales=function(){return beforeRegisterFilter().filter(event=>!state?.shared||state.account.role!=='owner'||!filters.register_id||Number(event.register_id??event.source?.register_id??1)===Number(filters.register_id));};
  const beforeRegisterReports=renderReports;
  renderReports=function(){
    beforeRegisterReports();if(!state?.shared)return;
    if(state.account.role==='owner'){
      $('content').querySelector('section .fields').insertAdjacentHTML('beforeend',`<label>${text('register')}<select id="reportRegister"><option value="">${t('all')}</option>${state.registers.map(item=>`<option value="${item.id}" ${String(filters.register_id||'')===String(item.id)?'selected':''}>${esc(item.name)}</option>`).join('')}</select></label>`);
      $('reportRegister').onchange=event=>{filters.register_id=event.target.value;renderReports();};
    }
    const tableElement=$('content').querySelector('section:last-child table');
    if(tableElement){const header=document.createElement('th');header.textContent=text('register');tableElement.querySelector('thead tr').append(header);const events=filteredSales();tableElement.querySelectorAll('tbody tr').forEach((row,index)=>{const cell=document.createElement('td');const register=events[index]?.register_id??events[index]?.source?.register_id??1;cell.textContent=state.registers.find(item=>item.id===register)?.name||String(register);row.append(cell);});}
  };
  const oldSettings=renderSettings;
  renderSettings=function(){
    oldSettings();if(!state?.shared)return;
    if(state.account.role==='cashier'){const templateForm=$('templateForm');templateForm.elements.shop_name.readOnly=true;templateForm.remove();$('content').replaceChildren(templateForm);return;}
    $('content').insertAdjacentHTML('beforeend',`<section class="card"><h2>${text('accounts')}</h2><div id="cashierAccounts"></div><form id="cashierAccountForm"><label>${text('username')}<input name="login" required maxlength="80" autocomplete="off"></label><label>${text('password')}<input name="password" type="password" required minlength="10" maxlength="256" autocomplete="new-password"></label><p class="muted">${text('passwordHint')}</p><label>${text('register')}<select name="register_id">${state.registers.map(item=>`<option value="${item.id}">${esc(item.name)}</option>`).join('')}</select></label><button type="submit">${text('addCashier')}</button></form></section>`);
    const showUsers=async()=>{const users=await api('users');if($('cashierAccounts'))$('cashierAccounts').innerHTML=table([text('username'),text('register')],users.map(user=>tr([esc(user.login),esc(state.registers.find(item=>item.id===user.register_id)?.name||text('owner'))])));};
    $('content').insertAdjacentHTML('beforeend',`<form id="storeTimeZone" class="card"><h2>Часовой пояс магазина / Store time zone</h2><select name="time_zone">${['Africa/Cairo','UTC','Europe/Moscow','Europe/Minsk','Europe/Istanbul'].map(zone=>opt(zone,zone,state.options.time_zone)).join('')}</select><button type="submit">${t('save')}</button></form>`);
    $('storeTimeZone').onsubmit=event=>{event.preventDefault();action(async()=>{await api('options',{time_zone:event.target.elements.time_zone.value});await refresh();renderSettings();notice(t('saved'));});};
    action(showUsers);
    $('cashierAccountForm').onsubmit=event=>{event.preventDefault();const form=event.target;action(async()=>{await api('users',{login:form.elements.login.value,password:form.elements.password.value,role:'cashier',register_id:Number(form.elements.register_id.value)});form.reset();await showUsers();notice(text('created'));});};
  };
  setInterval(async()=>{
    if(!state?.shared||page!=='reports'||document.querySelector('dialog[open]')||document.activeElement?.matches('#content input,#content select,#content textarea'))return;
    try{const updated=await api('sales');if(page==='reports'&&!document.querySelector('dialog[open]')){sales=updated;renderReports();}}catch(error){notice(error.message,true);}
  },10000);
  window.addEventListener('pywebviewready',()=>{const button=$('connectionSetup');if(button)button.hidden=state?.account?.role==='cashier';});
  const meEpoch=window.aliBabaAuthEpoch;
  fetch('/api/me').then(async response=>{
    if(meEpoch!==window.aliBabaAuthEpoch)return;
    if(response.status===401){window.aliBabaShowLogin();return;}
    if(response.ok)window.aliBabaSession=await response.json();
  }).catch(error=>notice(error.message,true));
})();
