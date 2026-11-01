'use strict';

// USB scanners in keyboard (HID) mode type a barcode followed by Enter.
// Scan controls only choose or prefill a product; saving stock stays explicit.
(() => {
  const scannerWords = {
    ru: {
      receivingScan: 'Сканировать товар для поступления',
      receivingScanHint: 'Нажмите на поле и сканируйте штрихкод. После Enter товар выберется; затем укажите количество и закупочную цену.',
      findScannedProduct: 'Найти по штрихкоду',
      scanFirst: 'Сначала сканируйте или введите штрихкод.',
      scanSelected: 'Выбран товар',
      scanUnknown: 'Товар с таким штрихкодом не найден',
      scanCreate: 'Создать карточку с этим штрихкодом',
      scanNewHint: 'Сканируйте код производителя в поле штрихкода. После Enter заполните название, единицу и продажную цену, затем сохраните карточку.',
      scanProductBarcode: 'Сканировать штрихкод нового товара',
      scanDuplicate: 'Этот штрихкод уже есть у товара',
      scanUseReceiving: 'Для увеличения остатка используйте «Поступление товара».',
      scanInvalid: 'Штрихкод: до 40 латинских букв и цифр, без пробелов.',
      cashierScanHint: 'Сканер: режим USB-клавиатуры (HID), Enter после кода. Каждое повторное сканирование добавляет одну штуку; для весового товара укажите фактический вес.',
      focusCashierScan: 'Готов к сканированию',
    },
    en: {
      receivingScan: 'Scan a product to receive stock',
      receivingScanHint: 'Click the field and scan a barcode. Enter selects the product; then enter the received quantity and purchase cost.',
      findScannedProduct: 'Find by barcode',
      scanFirst: 'Scan or enter a barcode first.',
      scanSelected: 'Selected product',
      scanUnknown: 'No product has this barcode',
      scanCreate: 'Create a product with this barcode',
      scanNewHint: 'Scan the manufacturer barcode into the barcode field. After Enter, fill in the name, unit and selling price, then save the product.',
      scanProductBarcode: 'Scan a new product barcode',
      scanDuplicate: 'This barcode already belongs to',
      scanUseReceiving: 'Use “Receive stock” to increase its stock.',
      scanInvalid: 'Barcode: up to 40 ASCII letters and digits, without spaces.',
      cashierScanHint: 'Scanner: USB keyboard (HID) mode, Enter after the code. Each repeated scan adds one piece; enter the actual weight for weighed goods.',
      focusCashierScan: 'Ready to scan',
    },
    ar: {
      receivingScan: 'مسح منتج لاستلام المخزون',
      receivingScanHint: 'انقر الحقل وامسح الباركود. يحدد Enter المنتج؛ ثم أدخل الكمية المستلمة وسعر الشراء.',
      findScannedProduct: 'بحث بالباركود',
      scanFirst: 'امسح الباركود أو أدخله أولاً.',
      scanSelected: 'المنتج المحدد',
      scanUnknown: 'لا يوجد منتج بهذا الباركود',
      scanCreate: 'إنشاء منتج بهذا الباركود',
      scanNewHint: 'امسح باركود الشركة المصنعة في حقل الباركود. بعد Enter أدخل الاسم والوحدة وسعر البيع، ثم احفظ المنتج.',
      scanProductBarcode: 'مسح باركود منتج جديد',
      scanDuplicate: 'هذا الباركود مستخدم للمنتج',
      scanUseReceiving: 'استخدم «استلام المخزون» لزيادة الكمية.',
      scanInvalid: 'الباركود: حتى 40 حرفاً لاتينياً أو رقماً دون مسافات.',
      cashierScanHint: 'الماسح: وضع لوحة مفاتيح USB ‏(HID) مع Enter بعد الرمز. كل مسح متكرر يضيف قطعة واحدة؛ أدخل الوزن الفعلي للسلع الموزونة.',
      focusCashierScan: 'جاهز للمسح',
    },
  };
  for (const language of ['ru', 'en', 'ar']) Object.assign(words[language], scannerWords[language]);
  const codeOf = value => String(value || '').trim().toUpperCase();
  const validCode = value => /^[A-Z0-9]{1,40}$/.test(value);
  let pendingReceiptBarcode = null;

  const renderInventoryBeforeScanner = renderInventory;
  renderInventory = function () {
    renderInventoryBeforeScanner();
    const receiving = $('batchForm'), productForm = $('productForm');
    const productSelect = receiving.elements.product;
    receiving.querySelector('button:not([type])').type = 'submit';
    productForm.querySelector('button:not([type])').type = 'submit';
    const previousProduct = productSelect.value;
    productSelect.insertAdjacentHTML('afterbegin', `<option value="">${t('choose')}</option>`);
    productSelect.value = previousProduct;
    receiving.querySelector('h2').insertAdjacentHTML('afterend', `<label>${t('receivingScan')}<input id="batchScan" type="text" maxlength="40" autocomplete="off" spellcheck="false" dir="ltr" aria-describedby="batchScanHint batchScanStatus"></label><button id="batchScanLookup" class="secondary" type="button">${t('findScannedProduct')}</button><p id="batchScanHint" class="muted">${t('receivingScanHint')}</p><p id="batchScanStatus" role="status" aria-live="polite"></p><button id="batchScanCreate" class="secondary" type="button" hidden>${t('scanCreate')}</button>`);
    const scan = $('batchScan'), status = $('batchScanStatus'), create = $('batchScanCreate');
    let unknownCode = '';
    const resetStatus = () => {
      status.textContent = '';
      status.className = '';
      create.hidden = true;
      unknownCode = '';
    };
    const selected = product => {
      productSelect.value = product.id;
      scan.value = '';
      resetStatus();
      status.textContent = `${t('scanSelected')}: ${product.name} · ${product.barcode}`;
      receiving.elements.qty.focus();
      receiving.elements.qty.select();
    };
    const lookup = () => {
      const code = codeOf(scan.value);
      scan.value = code;
      resetStatus();
      productSelect.value = '';
      if (!validCode(code)) {
        status.textContent = t(code ? 'scanInvalid' : 'scanFirst');
        status.className = 'error';
        scan.focus();
        return;
      }
      const product = state.products.find(item => codeOf(item.barcode) === code);
      if (product) {
        selected(product);
        return;
      }
      unknownCode = code;
      status.textContent = `${t('scanUnknown')}: ${code}`;
      status.className = 'error';
      create.hidden = false;
    };
    scan.oninput = () => { productSelect.value = ''; resetStatus(); };
    scan.onkeydown = event => {
      if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); lookup(); }
    };
    $('batchScanLookup').onclick = lookup;
    productSelect.addEventListener('change', () => { scan.value = ''; resetStatus(); });
    create.onclick = () => {
      if (!validCode(unknownCode)) return;
      const code = unknownCode;
      pendingReceiptBarcode = code;
      // A clean form prevents overwriting a previously edited product.
      renderInventory();
      const form = $('productForm');
      form.elements.barcode.value = code;
      form.scrollIntoView({behavior: 'smooth', block: 'center'});
      form.elements.name.focus();
    };

    const barcode = productForm.elements.barcode;
    barcode.parentElement.insertAdjacentHTML('beforeend', `<button id="scanProductBarcode" type="button" class="secondary">${t('scanProductBarcode')}</button><small class="muted">${t('scanNewHint')}</small><span id="productScanStatus" role="status" aria-live="polite"></span>`);
    $('scanProductBarcode').onclick = () => { barcode.focus(); barcode.select(); };
    barcode.addEventListener('keydown', event => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      event.stopPropagation();
      barcode.value = codeOf(barcode.value);
      const text = $('productScanStatus');
      text.textContent = '';
      if (!validCode(barcode.value)) {
        text.textContent = t(barcode.value ? 'scanInvalid' : 'scanFirst');
        return;
      }
      const duplicate = state.products.find(item => codeOf(item.barcode) === barcode.value && item.id !== productForm.dataset.edit);
      if (duplicate) {
        text.textContent = `${t('scanDuplicate')}: ${duplicate.name}. ${t('scanUseReceiving')}`;
        return;
      }
      const next = productForm.elements.name.value.trim() ? productForm.elements.price : productForm.elements.name;
      next.focus();
      next.select();
    });
    barcode.addEventListener('input', () => { $('productScanStatus').textContent = ''; });

    const newlyCreated = pendingReceiptBarcode && state.products.find(item => codeOf(item.barcode) === pendingReceiptBarcode);
    if (newlyCreated) {
      pendingReceiptBarcode = null;
      selected(newlyCreated);
    } else {
      scan.focus();
    }
  };

  const renderPOSBeforeScanner = renderPOS;
  renderPOS = function () {
    renderPOSBeforeScanner();
    $('scanForm').insertAdjacentHTML('afterend', `<p id="cashierScanHint" class="muted">${t('cashierScanHint')}</p>`);
    $('scan').setAttribute('aria-describedby', 'cashierScanHint');
    $('scanForm').onsubmit = event => {
      event.preventDefault();
      const input = $('scan'), query = input.value.trim();
      const product = state.products.find(item => codeOf(item.barcode) === codeOf(query)) ||
        state.products.find(item => item.name.toLowerCase() === query.toLowerCase());
      if (product) {
        addToCart(product);
      } else {
        notice(t(query ? 'scanUnknown' : 'scanFirst') + (query ? `: ${query}` : ''), true);
        input.focus();
        input.select();
      }
    };
  };
})();
