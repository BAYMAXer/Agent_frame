"""Read-only DOM hit testing and feedback for one dispatched scroll action."""
from __future__ import annotations

import math


# Inspect several locations inside the same element, then dispatch only the one
# original action. A nested button/link is a separate target, not part of a card.
ACTIONABILITY_INSPECT = r"""function(listenerScanComplete,...registeredListeners) {
  let target=this;
  if (!target.isConnected || !(target instanceof Element)) return {error:'detached'};
  // 自定义复选框/单选：原生 input 常被 CSS 隐藏（display:none / visibility:hidden / 零尺寸），
  // 真正可点击的是关联的可见 label。此时把点击目标重定向到 label，点击 label 会切换原生 input。
  if (target.matches('input[type="checkbox"],input[type="radio"],[role="checkbox"],[role="radio"]')) {
    const r0=target.getBoundingClientRect(), s0=getComputedStyle(target);
    if (r0.width<=0 || r0.height<=0 || s0.visibility!=='visible' || s0.display==='none') {
      let label=null;
      if (target.id) {
        const labelled=document.querySelectorAll('label[for]');
        for (let i=0;i<labelled.length;i++){ if (labelled[i].getAttribute('for')===target.id){ label=labelled[i]; break; } }
      }
      if (!label) label=target.closest('label');
      if (label) target=label;
    }
  }
  if (target.matches(':disabled') || target.getAttribute('aria-disabled')==='true' || target.closest('[inert]')) return {error:'disabled'};
  const rect=target.getBoundingClientRect(), style=getComputedStyle(target);
  if (rect.width<=0 || rect.height<=0 || style.visibility!=='visible' || style.display==='none') return {error:'hidden'};
  // DOM attributes do not reveal addEventListener handlers. The executor passes
  // actual descendant node objects obtained through CDP's read-only debugger API.
  if(!listenerScanComplete && (target.childElementCount>0 || target.shadowRoot?.childElementCount>0))return {error:'listener_inspection_unavailable'};
  const listenerElements=new Set(registeredListeners);
  const parent=el=>el.parentElement || (el.getRootNode && el.getRootNode().host) || null;
  const within=(root,el)=>{for(let i=0;el && i<64;i++,el=parent(el))if(el===root)return true;return false};
  const independent=el=>listenerElements.has(el) || el.hasAttribute('onclick') || typeof el.onclick==='function' ||
    el.matches('a[href],button,input,select,textarea,summary,label[for],[contenteditable]:not([contenteditable="false"]),[tabindex]:not([tabindex="-1"]),[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="switch"],[role="combobox"],[role="textbox"],[role="searchbox"],[role="menuitem"],[role="option"],[role="tab"]');
  const nestedControl=el=>{for(let i=0;el && el!==target && i<64;i++,el=parent(el))if(independent(el))return el;return null};
  const candidates=[],seen=new Set();
  const addRect=(r,source,grid=false)=>{
    const left=Math.max(0,r.left),right=Math.min(innerWidth,r.right),top=Math.max(0,r.top),bottom=Math.min(innerHeight,r.bottom);
    if(right-left<1 || bottom-top<1)return;
    const fractions=grid?[[.5,.5],[.2,.5],[.8,.5],[.5,.2],[.5,.8]]:[[.5,.5]];
    for(const [fx,fy] of fractions){
      if(candidates.length>=24)return;
      const x=left+(right-left)*fx,y=top+(bottom-top)*fy,key=x.toFixed(2)+','+y.toFixed(2);
      if(!seen.has(key)){seen.add(key);candidates.push({x,y,source})}
    }
  };
  for(const heading of Array.from(target.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"]')).slice(0,3)){
    if(!nestedControl(heading))for(const r of Array.from(heading.getClientRects()).slice(0,2))addRect(r,'heading');
  }
  const walker=document.createTreeWalker(target,NodeFilter.SHOW_TEXT);
  for(let n=walker.nextNode(),visited=0;n && visited<64 && candidates.length<10;n=walker.nextNode(),visited++){
    if(!n.textContent.trim() || nestedControl(n.parentElement))continue;
    const range=document.createRange();range.selectNodeContents(n);
    for(const r of Array.from(range.getClientRects()).slice(0,2))addRect(r,'text');
  }
  for(const r of Array.from(target.getClientRects()).slice(0,3))addRect(r,'client_rect',true);
  addRect(rect,'bounding_rect',true);
  let blocker=null,blockedPoint=null,relationship='unrelated';
  for(const point of candidates){
    let hit=document.elementFromPoint(point.x,point.y);
    for(let i=0;hit && hit.shadowRoot && hit.shadowRoot.elementFromPoint && i<16;i++){
      const next=hit.shadowRoot.elementFromPoint(point.x,point.y);if(!next || next===hit)break;hit=next;
    }
    const own=hit && within(target,hit),child=own?nestedControl(hit):null;
    if(own && !child)return {x:point.x,y:point.y,w:rect.width,h:rect.height,tag:target.tagName.toLowerCase(),readOnly:!!target.readOnly,
      editable:target.isContentEditable || target.tagName==='TEXTAREA' || (target.tagName==='INPUT' && !['button','submit','checkbox','radio','file'].includes(target.type)),
      point_source:point.source,candidates_checked:candidates.indexOf(point)+1};
    if(!blocker){blocker=child || hit;blockedPoint=point;relationship=child?'nested_control':'unrelated'}
  }
  return {error:'occluded',point:blockedPoint,
    blocker:blocker?{id:blocker.id,tag:blocker.tagName.toLowerCase(),role:blocker.getAttribute('role'),name:(blocker.getAttribute('aria-label') || blocker.innerText || '').slice(0,300)}:null,
    relationship,candidates_checked:candidates.length};
}"""


# Keep these exact DOM objects across before/after measurements. Re-running an
# ancestor query after wheel could silently measure a different element.
SCROLL_CHAIN = r"""function(x,y) {
  const root=document.scrollingElement || document.documentElement;
  let start=this instanceof Element?this:null;
  if(!start && Number.isFinite(x) && Number.isFinite(y)){
    start=document.elementFromPoint(x,y);
    for(let i=0;start?.shadowRoot?.elementFromPoint && i<16;i++){
      const next=start.shadowRoot.elementFromPoint(x,y);if(!next || next===start)break;start=next;
    }
  }
  const chain=[],seen=new Set();
  for(let el=start,i=0;el && i<64;i++,el=el.parentElement || el.getRootNode?.().host){
    if(el===root)break;
    if(!(el instanceof Element) || seen.has(el))continue;
    seen.add(el);
    if(/^(auto|scroll|overlay)$/.test(getComputedStyle(el).overflowY) && el.scrollHeight>el.clientHeight+.5)chain.push(el);
  }
  if(root)chain.push(root);
  return chain;
}"""

SCROLL_METRICS = r"""function() {
  const root=document.scrollingElement || document.documentElement;
  return Array.from(this).map((el,index)=>({index,connected:!!el.isConnected,
    scrollTop:el.scrollTop,clientHeight:el.clientHeight,scrollHeight:el.scrollHeight,
    smooth:getComputedStyle(el).scrollBehavior==='smooth',
    container:{kind:el===root?'page':'element',tag:el.tagName.toLowerCase(),id:el.id || '',
      role:el.getAttribute('role') || '',name:(el.getAttribute('aria-label') || el.getAttribute('title') || '').slice(0,160)}}));
}"""


def scroll_feedback(before, after, requested_delta):
    """Describe observed movement; unavailable metrics never mean a no-op."""
    unavailable = {'available': False, 'requested_delta': requested_delta,
                   'actual_delta': None, 'moved': None, 'noop': None,
                   'at_top': None, 'at_bottom': None, 'target_container': None,
                   'containers': []}
    if not before or not isinstance(before, list) or not isinstance(after, list) or len(before) != len(after):
        return unavailable
    containers = []
    for old, new in zip(before, after):
        if not isinstance(old, dict) or not isinstance(new, dict):
            return unavailable
        if old.get('connected') is False or new.get('connected') is False:
            return unavailable
        values = [old.get('scrollTop'), new.get('scrollTop'), new.get('clientHeight'), new.get('scrollHeight')]
        if not all(type(value) in (int, float) and math.isfinite(value) for value in values):
            return unavailable
        delta = new['scrollTop'] - old['scrollTop']
        maximum = max(0, new['scrollHeight'] - new['clientHeight'])
        containers.append({'container': new.get('container', {}), 'actual_delta': round(delta, 3),
                           'moved': abs(delta) > .5, 'before': old['scrollTop'], 'after': new['scrollTop'],
                           'at_top': new['scrollTop'] <= .5, 'at_bottom': new['scrollTop'] >= maximum - .5})
    target = next((item for item in containers if item['moved']), containers[0])
    moved = any(item['moved'] for item in containers)
    return {'available': True, 'requested_delta': requested_delta,
            'actual_delta': target['actual_delta'], 'moved': moved, 'noop': not moved,
            'at_top': target['at_top'], 'at_bottom': target['at_bottom'],
            'target_container': target['container'], 'containers': containers}
