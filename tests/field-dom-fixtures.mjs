/* Small DOM for command/form behavior, not a substitute for a browser gate. */
export class Node {
  constructor(tag) {
    this.tagName=tag.toUpperCase(); this.children=[]; this.listeners={}; this.attributes={};
    this.value=''; this.disabled=false; this.readOnly=false; this.checked=false;
    this.hidden=false; this._text=''; this.className=''; this.style={};
    if(this.tagName==='SELECT') {
      Object.defineProperty(this,'options',{get:()=>this.children.filter(n=>n.tagName==='OPTION')});
      Object.defineProperty(this,'type',{get:()=>this.multiple?'select-multiple':'select-one'});
      Object.defineProperty(this,'value',{get:()=>this._selectedValue || '',set:value=>{
        this._selectedValue=this.options.some(o=>o.value===String(value))?String(value):'';
      }});
    }
  }
  append(...nodes) { for(const n of nodes.filter(Boolean)) { this.children.push(n); n.parentNode=this; } }
  appendChild(node) { this.append(node); return node; }
  replaceChildren(...nodes) { for(const child of this.children)child.parentNode=null; this.children=[]; this.append(...nodes); }
  get textContent() { return this._text+this.children.map(n=>n.textContent || '').join(''); }
  set textContent(value) { this._text=String(value); this.replaceChildren(); }
  setAttribute(key,value) { this.attributes[key]=String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  addEventListener(key,fn) { (this.listeners[key] ||= []).push(fn); }
  async dispatch(key,event={}) { for(const fn of this.listeners[key] || []) await fn(event); }
  focus() { this.ownerDocument.activeElement=this; }
  showModal() { this.open=true; }
  close() { this.open=false; }
  remove() { if(this.parentNode) this.parentNode.children=this.parentNode.children.filter(n=>n!==this); this.parentNode=null; }
  get isConnected() { return Boolean(this.parentNode?.isConnected || this === this.ownerDocument?.body); }
  findAll(predicate,result=[]) { for(const n of this.children) { if(predicate(n)) result.push(n); n.findAll?.(predicate,result); } return result; }
  find(predicate) { return this.findAll(predicate)[0] || null; }
  querySelector(selector) { return this.find(n=>selector.startsWith('#') ? n.id===selector.slice(1) : n.tagName.toLowerCase()===selector); }
}
export function documentFixture() {
  const document={activeElement:null,createElement(tag) { const n=new Node(tag); n.ownerDocument=document; return n; }};
  document.body=document.createElement('body'); document.app=document.createElement('main'); document.app.id='app';
  document.badge=document.createElement('span'); document.badge.id='mode-badge'; document.body.append(document.badge,document.app);
  document.querySelector=selector=>document.body.querySelector(selector);
  return document;
}
export const elementFor=document=>(tag,text=null,className='')=>{
  const n=document.createElement(tag); if(text!==null)n.textContent=text; n.className=className; return n;
};
export function localInput(value) {
  const d=new Date(value); return new Date(d.valueOf()-d.getTimezoneOffset()*60000).toISOString().slice(0,23);
}
export const byId=(root,id)=>root.querySelector(`#field-${id}`);
export const byText=(root,text)=>root.find(n=>n.textContent===text);
