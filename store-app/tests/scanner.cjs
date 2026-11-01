const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('assert');
const BASE_URL=process.env.STORE_TEST_URL || 'http://127.0.0.1:8767';
if(!['127.0.0.1','localhost'].includes(new URL(BASE_URL).hostname))throw Error('Scanner tests require a local isolated database');
(async()=>{
 const browser=await chromium.launch({...(process.env.CHROMIUM_EXECUTABLE?{executablePath:process.env.CHROMIUM_EXECUTABLE}:{}),args:['--no-sandbox','--disable-dev-shm-usage']});
 try {
  const p=await browser.newPage();let errors=[],mutations=[];
  p.on('pageerror',error=>errors.push(error.message));
  p.on('request',request=>{if(request.method()==='POST')mutations.push(new URL(request.url()).pathname);});
  await p.goto(BASE_URL);await p.locator('#scan').waitFor();
  const getState=()=>p.evaluate(async()=>await(await fetch('/api/state')).json());
  const initial=await getState();assert.equal(initial.products.length,0,'Refusing to change existing product data');assert.equal(initial.shifts.length,0,'Use a fresh isolated database');
  const post=(path,data)=>p.evaluate(async({path,data})=>{const response=await fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const result=await response.json();if(!response.ok)throw Error(result.error);return result;},{path,data});
  await post('products',{name:'Alpha product',barcode:'ABC100',unit:'piece',price:10});
  await post('products',{name:'Beta product',barcode:'BETA200',unit:'piece',price:7});
  await p.reload();await p.locator('#scan').waitFor();
  await p.locator('[data-page=inventory]').click();await p.locator('#batchScan').waitFor();
  const scan=p.locator('#batchScan'),selection=p.locator('#batchForm [name=product]');
  const beta=(await getState()).products.find(product=>product.barcode==='BETA200');
  let beforeMutations=mutations.length;
  await scan.fill(' beta200 ');await scan.press('Enter');
  assert.equal(await selection.inputValue(),beta.id,'HID Enter chooses exact normalized barcode');
  assert(await p.locator('#batchForm [name=qty]').evaluate(element=>element===document.activeElement),'known scan moves focus to quantity');
  assert.equal(mutations.length,beforeMutations,'scan selection does not submit a batch or create a product');
  assert.equal((await getState()).batches.length,0);
  await p.locator('#batchForm [name=qty]').fill('2');await p.locator('#batchForm [name=cost]').fill('3');
  await p.locator('#batchForm button[type=submit]').click();
  await p.waitForFunction(()=>state.batches.length===1);
  let current=await getState();assert.equal(current.batches.length,1);assert.equal(current.batches[0].product,beta.id);assert.equal(current.batches[0].qty,2000);
  // Repeated scans choose a product, rather than silently receiving stock.
  beforeMutations=mutations.length;
  await scan.fill('BETA200');await scan.press('Enter');await scan.fill('BETA200');await scan.press('Enter');
  assert.equal(mutations.length,beforeMutations);assert.equal((await getState()).products.find(product=>product.id===beta.id).stock,2);
  // A new/invalid scan clears the old selection, so Save cannot receive the wrong item.
  await scan.fill('NEW300');assert.equal(await selection.inputValue(),'');await scan.press('Enter');
  await p.locator('#batchScanCreate').waitFor({state:'visible'});
  assert.equal(await selection.inputValue(),'');assert.equal((await getState()).products.length,2);assert.equal(mutations.length,beforeMutations);
  await p.locator('#batchScanCreate').click();
  const product=p.locator('#productForm');assert.equal(await product.locator('[name=barcode]').inputValue(),'NEW300');
  assert(!(await product.getAttribute('data-edit')),'unknown product must use a clean new-card form');
  assert.equal(await selection.inputValue(),'','creating a draft must not silently reselect the first existing product');
  assert.equal((await getState()).products.length,2,'explicit create action only prefills the card');
  // New-card scanner action + Enter must not accidentally submit the form.
  await p.locator('#scanProductBarcode').click();assert(await product.locator('[name=barcode]').evaluate(element=>element===document.activeElement));
  await product.locator('[name=barcode]').fill(' new300 ');await product.locator('[name=barcode]').press('Enter');
  assert.equal(await product.locator('[name=barcode]').inputValue(),'NEW300');
  assert(await product.locator('[name=name]').evaluate(element=>element===document.activeElement));
  assert.equal(mutations.length,beforeMutations);
  await product.locator('[name=name]').fill('New scanned product');await product.locator('[name=price]').fill('5');
  await product.locator('button[type=submit]').click();await p.waitForFunction(()=>state.products.length===3);
  current=await getState();const created=current.products.find(product=>product.barcode==='NEW300');assert(created);
  assert.equal(await selection.inputValue(),created.id,'created card is selected for the pending receipt');
  assert.equal(created.stock,0,'card creation does not silently receive inventory');
  // A manual selection explicitly cancels an unresolved receiving draft.
  await scan.fill('DRAFT400');await scan.press('Enter');await p.locator('#batchScanCreate').click();
  assert.equal(await selection.inputValue(),'','an unresolved draft keeps the receiving selection empty');
  await selection.selectOption(beta.id);assert.equal(await selection.inputValue(),beta.id,'operator can explicitly select another existing product');
  await product.locator('[name=name]').fill('Manually abandoned receiving draft');await product.locator('[name=price]').fill('4');
  await product.locator('button[type=submit]').click();await p.waitForFunction(()=>state.products.length===4);
  assert.notEqual(await selection.inputValue(),(await getState()).products.find(product=>product.barcode==='DRAFT400').id,'manual selection cancels deferred selection of the draft product');
  // Duplicate scans in the new-card form identify the existing product safely.
  await product.locator('[name=barcode]').fill('ABC100');await product.locator('[name=barcode]').press('Enter');
  assert((await p.locator('#productScanStatus').textContent()).includes('Alpha product'));assert.equal((await getState()).products.length,4);
  // Cashier workflow: each Enter adds one piece, refocuses, and unknown scans do not alter the cart.
  await p.locator('[data-page=pos]').click();const cashier=p.locator('#scan');
  await cashier.fill(' beta200 ');await cashier.press('Enter');assert.equal(await p.locator('[data-qty]').inputValue(),'1');
  assert(await cashier.evaluate(element=>element===document.activeElement));
  await cashier.fill('BETA200');await cashier.press('Enter');assert.equal(await p.locator('[data-qty]').inputValue(),'2');
  beforeMutations=mutations.length;
  await cashier.fill('MISSING999');await cashier.press('Enter');assert.equal(await p.locator('[data-qty]').inputValue(),'2');
  assert.equal(mutations.length,beforeMutations);assert.equal((await getState()).products.length,4);
  assert(await cashier.evaluate(element=>element.selectionStart===0&&element.selectionEnd===element.value.length),'unknown scan is selected for safe replacement');
  // Scanner controls survive language rerenders and RTL does not reverse barcode entry.
  await p.locator('#language').selectOption('ar');await p.locator('[data-page=inventory]').click();
  assert.equal(await p.locator('html').getAttribute('dir'),'rtl');assert.equal(await scan.getAttribute('dir'),'ltr');
  await scan.fill('abc100');await scan.press('Enter');assert((await p.locator('#batchScanStatus').textContent()).includes('Alpha product'));
  assert.deepEqual(errors,[]);
  console.log('PASS: receiving HID scans and explicit stock save, unknown-code creation prefill, new-card Enter safety, duplicate detection, cashier repeat/unknown scans, Arabic rerender');
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
