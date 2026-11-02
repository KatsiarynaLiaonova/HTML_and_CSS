const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {spawn}=require('node:child_process');
const readline=require('node:readline');
const assert=require('node:assert/strict');
(async()=>{
 const fixture=spawn(process.env.STORE_TEST_PYTHON||'python3',['tests/shared_fixture.py'],{stdio:['pipe','pipe','inherit']});
 let browser;
 try{
  const addresses=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Shared test fixture did not start')),25000);const lines=readline.createInterface({input:fixture.stdout});lines.once('line',line=>{clearTimeout(timer);resolve(JSON.parse(line));});fixture.once('exit',code=>{clearTimeout(timer);reject(Error('Shared fixture exited: '+code));});});
  browser=await chromium.launch({...(process.env.CHROMIUM_EXECUTABLE?{executablePath:process.env.CHROMIUM_EXECUTABLE}:{}),args:['--no-sandbox','--disable-dev-shm-usage']});
  const pages=[],errors=[];
  async function login(url,user,password){
   const context=await browser.newContext({timezoneId:user==='owner'?'America/New_York':'Africa/Cairo'});await context.route('**/*',route=>['127.0.0.1','localhost'].includes(new URL(route.request().url()).hostname)?route.continue():route.abort());const p=await context.newPage();pages.push(p);p.on('pageerror',e=>errors.push(e.message));
   await p.goto(url);await p.locator('#loginForm').waitFor();await p.locator('#loginForm [name=login]').fill(user);await p.locator('#loginForm [name=password]').fill(password);await p.locator('#loginForm button').click();await p.locator('#accountBar').waitFor();return p;
  }
  const owner=await login(addresses.owner,'owner','Owner browser password!');
  const first=await login(addresses.first,'cashier1','Cashier one password!');
  const second=await login(addresses.second,'cashier2','Cashier two password!');
  for(const p of pages)assert.equal(await p.evaluate(()=>reportDate('2026-11-01T22:30:00Z')),'2026-11-02','report day uses the store time zone even when the owner travels');
  const call=(p,route,data)=>p.evaluate(async({route,data})=>await api(route,data),{route,data});
  const state=p=>call(p,'state');
  const goto=(p,page)=>p.locator(`[data-page=${page}]`).click();
  const fresh=async p=>{await p.locator('#refreshShared').click();await p.waitForFunction(()=>state.products.length===1);};
  await call(owner,'rates',{EUR:.9,EGP:50,RUB:90});
  const created=await call(owner,'products',{name:'Shared test coffee',barcode:'SHARED123',unit:'piece',price:10});
  const product=created.products[0];await call(owner,'batches',{product:product.id,qty:3,cost:100});
  await fresh(first);await fresh(second);
  for(const [p,opening]of [[first,100],[second,200]]){
   await goto(p,'shifts');await p.locator('#openShift [name=EGP]').fill(String(opening));await p.locator('#openShift button').click();await p.locator('#closeShift').waitFor();
   assert.equal((await state(p)).shifts.length,1,'cashier sees only its own shift');
   await goto(p,'pos');await p.locator('#scan').fill('SHARED123');await p.locator('#scan').press('Enter');await p.locator('#currency').selectOption('EGP');
  }
  await Promise.all([first.locator('#checkout').click(),second.locator('#checkout').click()]);
  for(const p of [first,second])await p.locator('#lastReceipt').waitFor();
  const firstState=await state(first),secondState=await state(second);
  assert.equal(firstState.products[0].stock,1);assert.equal(secondState.products[0].stock,1);
  assert.equal(firstState.shifts[0].summary.expected.EGP,600);assert.equal(secondState.shifts[0].summary.expected.EGP,700);
  assert.equal((await call(owner,'sales')).length,2,'owner sees both sales');assert.equal((await call(first,'sales')).length,1);assert.equal((await call(second,'sales')).length,1);
  // Owner price changes must reject an already-open cart until its operator refreshes.
  await first.locator('#scan').fill('SHARED123');await first.locator('#scan').press('Enter');
  await call(owner,'product-update',{id:product.id,name:product.name,barcode:product.barcode,price:20});
  await first.locator('#checkout').click();await first.getByText(/Prices changed/).waitFor();
  assert.equal((await call(owner,'sales')).length,2);assert.equal((await state(first)).products[0].stock,1);
  await first.locator('#refreshShared').click();await first.waitForFunction(()=>state.products[0].price===20);await first.locator('[data-remove]').click();
  // A cashier can calibrate its own printer without changing the other register.
  await goto(second,'settings');assert.equal(await second.locator('#ratesForm').count(),0);assert.equal(await second.locator('#restoreForm').count(),0);
  await second.locator('#labelRotation').selectOption('270');
  await second.locator('#templateForm button:not(#testLabel)').click();await second.waitForFunction(()=>state.options.template.labelRotation===270);
  assert.equal((await state(first)).options.template.labelRotation,0);assert.equal((await state(second)).options.template.labelRotation,270);
  const denied=await second.evaluate(async()=>{const response=await fetch('/api/options',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':window.aliBabaSession.csrf},body:JSON.stringify({shop_name:'Forbidden'})});return response.status;});assert.equal(denied,403);
  // Lose a successful sale response, reload the client and recover the same receipt.
  await goto(first,'pos');await first.locator('#scan').fill('SHARED123');await first.locator('#scan').press('Enter');
  await first.route('**/api/checkout',async route=>{const response=await route.fetch();assert.equal(response.status(),200);await route.abort('failed');},{times:1});
  await first.locator('#checkout').click();await first.locator('#recoverCheckout').waitFor();
  assert.equal((await call(owner,'sales')).length,3,'the server committed the sale despite the lost response');
  await first.reload();await first.locator('#recoverCheckout').waitFor();await first.locator('#recoverCheckout').click();await first.locator('#lastReceipt').waitFor();
  assert.equal((await call(owner,'sales')).length,3,'recovery must not duplicate the sale');
  assert.equal((await state(first)).products[0].stock,0);assert.equal((await state(first)).shifts[0].summary.expected.EGP,1600);
  await goto(owner,'reports');await owner.locator('#refreshShared').click();await owner.waitForFunction(()=>sales.length===3);
  assert((await owner.locator('.stats').textContent()).includes('2\u00a0000,00')||(await owner.locator('.stats').textContent()).includes('2000,00'),'owner reports include both cashiers');
  await owner.locator('#reportRegister').selectOption('2');assert.equal(await owner.locator('section:last-child tbody tr').count(),1,'owner can filter reports for the second register');await owner.locator('#reportRegister').selectOption('');
  await goto(first,'inventory');await first.locator('#productForm [name=name]').fill('Cashier-created card');await first.locator('#productForm [name=barcode]').fill('SCOPE999');await first.locator('#productForm [name=price]').fill('5');await first.locator('#productForm button[type=submit]').click();await first.waitForFunction(()=>state.products.length===2);
  assert.equal(await first.evaluate(()=>state.shared),true);assert.equal(await first.evaluate(()=>state.account.role),'cashier');assert.equal(await first.evaluate(()=>state.shifts.length),1);
  await goto(first,'settings');assert.equal(await first.locator('#ratesForm').count(),0,'inventory save preserves cashier scope');
  await first.locator('#logout').click();await first.locator('#loginForm').waitFor();assert.equal(await first.locator('[data-qty]').count(),0);
  assert.equal(await first.evaluate(async()=>await(await fetch('/api/state')).status),401);
  assert.deepEqual(errors,[]);
  console.log('PASS: main store and two verified HTTPS gateways; owner/cashier login, separate simultaneous shifts and cash, shared stock, combined owner reports, cashier-only history, stale-price rejection, per-register printer settings, lost-response recovery after reload without duplicate stock/cash, and logout');
 }finally{
  if(browser)await browser.close();fixture.stdin.end();await new Promise(resolve=>{if(fixture.exitCode!==null)return resolve();fixture.once('exit',resolve);setTimeout(()=>{fixture.kill();resolve();},10000).unref();});
 }
})().catch(error=>{console.error(error);process.exit(1);});
