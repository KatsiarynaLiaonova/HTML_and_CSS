// Run only against a fresh, isolated STORE_DATA_DIR. This creates a test product/sale.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');

const BASE_URL=process.env.STORE_TEST_URL || 'http://127.0.0.1:8767';
if(!['127.0.0.1','localhost'].includes(new URL(BASE_URL).hostname))throw Error('Printing tests require a local isolated database');
const output=process.env.PRINT_TEST_OUTPUT_DIR || path.join(os.tmpdir(),`ali-baba-printing-${process.pid}`);
fs.mkdirSync(output,{recursive:true});
const near=(actual,expected,message,tolerance=1)=>assert(Math.abs(actual-expected)<=tolerance,`${message}: ${actual} versus ${expected}`);

function pdfPages(bytes,expectedCount,widthMM,heightMM){
 const source=bytes.toString('latin1');
 const count=(source.match(/\/Type\s*\/Page\b/g)||[]).length;
 assert.equal(count,expectedCount,'one PDF page per printed label, without blank trailing pages');
 const boxes=[...source.matchAll(/\/MediaBox\s*\[\s*([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s*\]/g)].map(m=>m.slice(1).map(Number));
 assert(boxes.length,'Chromium PDF must contain explicit page dimensions');
 for(const [x0,y0,x1,y1]of boxes){
  near(x1-x0,widthMM*72/25.4,'PDF page width in points');
  if(heightMM!==undefined)near(y1-y0,heightMM*72/25.4,'PDF page height in points');
 }
 return boxes;
}

(async()=>{
 const browser=await chromium.launch({...(process.env.CHROMIUM_EXECUTABLE?{executablePath:process.env.CHROMIUM_EXECUTABLE}:{}),args:['--no-sandbox','--disable-dev-shm-usage','--proxy-bypass-list=127.0.0.1;localhost']});
 try{
  const p=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[];
  p.on('pageerror',e=>errors.push(e.message));
  const state=()=>p.evaluate(async()=>await(await fetch('/api/state')).json());
  const post=(route,data)=>p.evaluate(async({route,data})=>{
   const response=await fetch('/api/'+route,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
   const result=await response.json();if(!response.ok)throw Error(result.error);return result;
  },{route,data});
  const goto=page=>p.locator(`[data-page="${page}"]`).click();
  const closePrint=()=>p.locator('#closePrint').click();
  const saveTemplate=async rotation=>{
   const response=p.waitForResponse(r=>r.url().endsWith('/api/options')&&r.request().method()==='POST');
   await p.locator('#templateForm button:not(#testLabel)').click();
   assert((await response).ok(),'template save must succeed');
   await p.waitForFunction(value=>document.querySelector('#labelRotation')?.value===String(value),rotation);
   const saved=(await state()).options.template;
   assert.equal(saved.labelRotation,rotation,'rotation persisted in the database');
   assert.equal(saved.width,38,'physical/logical width setting is retained');
   assert.equal(saved.height,25,'physical/logical height setting is retained');
   await p.reload();await p.locator('#scan').waitFor();await goto('settings');
   assert.equal(await p.locator('#labelRotation').inputValue(),String(rotation),'rotation survives app reload');
  };
  const labelGeometry=async rotation=>{
   const measured=await p.locator('.label-sheet').first().evaluate(sheet=>{
    const inner=sheet.querySelector('.label-preview'),outer=sheet.getBoundingClientRect(),box=inner.getBoundingClientRect();
    const transform=new DOMMatrix(getComputedStyle(inner).transform);
    return {outer:{width:outer.width,height:outer.height},inner:{width:box.width,height:box.height},
     edges:[box.left-outer.left,box.top-outer.top,outer.right-box.right,outer.bottom-box.bottom],
     logical:{width:inner.offsetWidth,height:inner.offsetHeight},matrix:[transform.a,transform.b,transform.c,transform.d]};
   });
   const quarter=rotation===90||rotation===270,width=38,height=25;
   near(measured.outer.width,width*96/25.4,'sheet width');near(measured.outer.height,height*96/25.4,'sheet height');
   near(measured.logical.width,(quarter?25:38)*96/25.4,'artwork width within the fixed paper form');near(measured.logical.height,(quarter?38:25)*96/25.4,'artwork height within the fixed paper form');
   for(const edge of measured.edges)near(edge,0,'rotated artwork stays inside its page',1);
   const radians=rotation*Math.PI/180,expected=[Math.cos(radians),Math.sin(radians),-Math.sin(radians),Math.cos(radians)];
   measured.matrix.forEach((value,i)=>near(value,expected[i],'actual artwork rotation',.0001));
   return {width,height};
  };

  await p.goto(BASE_URL);await p.locator('#scan').waitFor();
  const initial=await state();
  assert.equal(initial.products.length,0,'Refusing to run against existing product data');
  assert.equal(initial.shifts.length,0,'Use an empty test database');
  assert.equal(initial.configured,false,'Calibration must work before exchange rates are configured');
  await goto('settings');

  // The preview uses unsaved form choices without silently changing saved settings.
  for(const rotation of [0,90,180,270]){
   const previous=(await state()).options.template;
   await p.locator('#templateForm [name=width]').fill('38');await p.locator('#templateForm [name=height]').fill('25');
   await p.locator('#labelRotation').selectOption(String(rotation));
   await p.locator('#testLabel').click();await p.locator('#printDialog').waitFor({state:'visible'});
   assert.equal(await p.locator('.label-sheet').count(),1,'calibration prints exactly one label');
   assert.equal(await p.locator('.label-calibration').count(),1,'calibration has a distinctive asymmetric design');
   assert.match(await p.locator('.label-calibration').innerText(),/ВЕРХ/);
   assert.match(await p.locator('.label-calibration').innerText(),/ЛЕВО/);
   assert.equal(await p.locator('#labelsPrinted:visible').count(),0,'calibration cannot confirm merchandise labels');
   const untouched=await state();assert.deepEqual(untouched.options.template,previous,'test print does not save settings');
   assert.equal(untouched.configured,false);assert.equal(untouched.products.length,0);
   await p.emulateMedia({media:'print'});
   assert.equal(await p.locator('#printDialog').evaluate(el=>getComputedStyle(el).borderRadius),'0px','print dialog must not clip square label corners');
   const size=await labelGeometry(rotation);
   const pdf=await p.pdf({path:path.join(output,`calibration-${rotation}.pdf`),preferCSSPageSize:true,printBackground:true});
   pdfPages(pdf,1,size.width,size.height);
   await p.emulateMedia({media:'screen'});
   await closePrint();await saveTemplate(rotation);
  }

  // Real merchandise labels use the same geometry; two copies remain two pages.
  await post('rates',{EUR:.9,EGP:50,RUB:90});
  const created=await post('products',{name:'LEFT → Coffee قهوة',barcode:'123456',unit:'piece',price:3});
  const product=created.products[0];
  await p.reload();await p.locator('#scan').waitFor();await goto('labelsBatch');
  await p.locator('[data-copies]').fill('2');await p.locator('#printLabels').click();
  assert.equal(await p.locator('.label-sheet').count(),2);assert.equal(await p.locator('.label-preview svg').count(),2);
  await p.emulateMedia({media:'print'});
  const size=await labelGeometry(270);
  const labels=await p.pdf({path:path.join(output,'two-labels-270.pdf'),preferCSSPageSize:true,printBackground:true});
  pdfPages(labels,2,size.width,size.height);
  await p.emulateMedia({media:'screen'});
  await closePrint();await goto('settings');await p.locator('#testLabel').click();
  assert.equal(await p.locator('#labelsPrinted:visible').count(),0,'a previous merchandise job cannot leak its confirmation into a test label');
  assert.equal((await state()).products[0].label_changed,true,'calibration does not mark prices printed');
  await closePrint();

  // Changing label rotation must not rotate or resize the receipt printer output.
  await post('batches',{product:product.id,qty:2,cost:1});
  await post('shift-open',{opening:{USD:0,EUR:0,EGP:0,RUB:0}});
  const sale=await post('checkout',{currency:'USD',method:'cash',items:[{id:product.id,qty:1}],tender:3});
  await p.evaluate(s=>printReceipt(s),sale);await p.locator('#printDialog').waitFor({state:'visible'});
  assert.equal(await p.locator('.label-sheet').count(),0);
  assert.equal(await p.locator('.receipt').evaluate(el=>getComputedStyle(el).transform),'none','receipt is not rotated');
  await p.emulateMedia({media:'print'});
  const receipt=await p.pdf({path:path.join(output,'receipt-after-rotated-label.pdf'),preferCSSPageSize:true,printBackground:true});
  pdfPages(receipt,1,76);
  assert.deepEqual(errors,[]);
  console.log('PASS: calibration before setup, all four rotations, saved settings, fixed physical paper dimensions, unclipped artwork bounds, correct PDF page sizes, two copies/two pages, price-label confirmation isolation, receipt printing unchanged');
  console.log('PDFs: '+output);
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
