'use strict';
function showBrand(){
 const b=state.bootstrap.brand;
 openDrawer(`<div class="archive-heading"><div><h1>器物与日常</h1></div></div>
 <section class="archive-hero"><img src="/assets/assets/bridge-family-v6.png" alt="Pont 26肩包与打开的Etui 04钥匙包"><div><p class="eyebrow">THE BRIDGE</p><h2>一座桥，<br>两种尺度。</h2><p>在肩包上，它是开合的扣件；收进掌心，它托住四枚钥匙挂钩。两端的圆头铆钉，让不同的器物有了相同的来处。</p></div></section>
 <div class="catalog-heading"><h2>商品档案</h2><span>${state.bootstrap.products.length} 件</span></div><div class="catalog-grid">${state.bootstrap.products.map(p=>`<button class="catalog-item" data-product-detail="${p.id}"><img src="/assets/assets/${esc(p.image)}" alt="${esc(p.name)}"><span>${esc(p.collection)}</span><h3>${esc(p.name)}</h3><small>¥${p.price_cny.toLocaleString()} · ${p.colors.join(' / ')}</small></button>`).join('')}</div>
 <details class="archive-history"><summary>工坊与设计沿革</summary><div>${b.history.map(h=>`<article><span>${h.year}</span><h3>${esc(h.title)}</h3><p>${esc(h.detail)}</p></article>`).join('')}</div></details>
 <details class="archive-service"><summary>价格与服务</summary><dl>${[['pricing','价格'],['repair','维修'],['personalization','个性化'],['packaging','礼赠包装']].map(([key,label])=>`<dt>${label}</dt><dd>${esc(b.policies[key])}</dd>`).join('')}</dl></details>
 `);
 $('#drawer').classList.add('archive-dialog');bindProductDetails();
}
function bindProductDetails(){$$('[data-product-detail]').forEach(b=>b.onclick=()=>showProduct(b.dataset.productDetail));}
function showProduct(id){
 const p=state.bootstrap.products.find(p=>p.id===id);if(!p)return;const gallery=p.gallery||[{image:p.image,caption:'整体轮廓'}];
 openDrawer(`<button id="back-catalog" class="quiet">← 商品档案</button><div class="product-detail"><div class="product-gallery"><img id="product-image" src="/assets/assets/${esc(gallery[0].image)}" alt="${esc(p.name)}">${gallery.length>1?`<div class="gallery-options">${gallery.map((image,i)=>`<button data-gallery="${i}" class="${i===0?'selected':''}"><img src="/assets/assets/${esc(image.image)}" alt=""><span>${esc(image.caption)}</span></button>`).join('')}</div>`:''}${p.design_notes?`<div class="design-notes">${p.design_notes.map((note,i)=>`<article><span>0${i+1}</span><div><h3>${esc(note.title)}</h3><p>${esc(note.detail)}</p></div></article>`).join('')}</div>`:''}</div><div class="product-copy"><p class="eyebrow">${esc(p.collection)}</p><h1>${esc(p.name)}</h1><div class="product-price">¥${p.price_cny.toLocaleString()}</div><p class="product-colors">${esc(p.colors.join(' · '))}</p><p class="product-story">${esc(p.value_story)}</p><dl class="product-facts"><dt>材质</dt><dd>${esc(p.material)}</dd><dt>尺寸 / 重量</dt><dd>${esc(p.dimensions_cm.join(' × '))} cm · 约 ${p.weight_g} g</dd><dt>收纳</dt><dd>${esc(p.capacity)}</dd><dt>携带</dt><dd>${esc(p.carry)}</dd></dl>${p.fit_checks?`<div class="fit-checks">${p.fit_checks.map(f=>`<p><strong>${esc(f.title)}</strong>${esc(f.detail)}</p>`).join('')}</div>`:''}<button id="use-product" class="primary" ${state.view==='workspace'&&state.customer?'':'disabled'}>围绕这款补充</button><details><summary>制作细节</summary><p>${esc(p.construction)}</p>${p.design_detail?`<p>${esc(p.design_detail)}</p>`:''}</details><details><summary>使用与护理</summary>${p.use_guide?`<p>${esc(p.use_guide)}</p>`:''}<p>${esc(p.care)}</p></details><details><summary>选择前需了解</summary>${p.limits.map(limit=>`<p>${esc(limit)}</p>`).join('')}</details></div></div>`);
 const notes=$('.design-notes');if(notes)$('.product-detail').append(notes);
 $('#drawer').classList.add('archive-dialog');$('#back-catalog').onclick=showBrand;$$('[data-gallery]').forEach(button=>button.onclick=()=>{const image=gallery[Number(button.dataset.gallery)];$('#product-image').src='/assets/assets/'+image.image;$('#product-image').alt=p.name+' · '+image.caption;$$('[data-gallery]').forEach(item=>item.classList.toggle('selected',item===button));});
 $('#use-product').onclick=()=>{
  const text=$('#advisor-note-text');if(!text)return;$('#drawer').close();
  const current=text.value;text.value=current?current+' '+p.name:'关于'+p.name+'，';text.dispatchEvent(new Event('input'));text.focus();

 };
}
