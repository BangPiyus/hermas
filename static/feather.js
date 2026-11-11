/* Feather-style SVG icon subset (MIT; Feather Icons, https://feathericons.com).
   Local, dependency-free renderer for the symbols used in this dashboard. */
(function(){
 const shapes={
 'cpu': [['rect',{x:4,y:4,width:16,height:16,rx:2}],['rect',{x:9,y:9,width:6,height:6}],['path',{d:'M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3'}]],
 'log-in':[['path',{d:'M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4'}],['polyline',{points:'10 17 15 12 10 7'}],['line',{x1:15,y1:12,x2:3,y2:12}]],
 'x':[['line',{x1:18,y1:6,x2:6,y2:18}],['line',{x1:6,y1:6,x2:18,y2:18}]],
 'plus':[['line',{x1:12,y1:5,x2:12,y2:19}],['line',{x1:5,y1:12,x2:19,y2:12}]],
 'clock':[['circle',{cx:12,cy:12,r:10}],['polyline',{points:'12 6 12 12 16 14'}]],
 'message-square':[['path',{d:'M21 11.5a8.38 8.38 0 0 1-.9 3.8A8.5 8.5 0 0 1 12.5 20a8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8A8.5 8.5 0 0 1 8.7 3.9a8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z'}]],
 'log-out':[['path',{d:'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4'}],['polyline',{points:'16 17 21 12 16 7'}],['line',{x1:21,y1:12,x2:9,y2:12}]],
 'menu':[['line',{x1:3,y1:6,x2:21,y2:6}],['line',{x1:3,y1:12,x2:21,y2:12}],['line',{x1:3,y1:18,x2:21,y2:18}]],
 'database':[['ellipse',{cx:12,cy:5,rx:9,ry:3}],['path',{d:'M3 5v14a9 3 0 0 0 18 0V5M3 12a9 3 0 0 0 18 0'}]],
 'layers':[['polygon',{points:'12 2 2 7 12 12 22 7 12 2'}],['polyline',{points:'2 17 12 22 22 17'}],['polyline',{points:'2 12 12 17 22 12'}]],
 'shield':[['path',{d:'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z'}]],
 'check-square':[['polyline',{points:'9 11 12 14 22 4'}],['path',{d:'M21 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11'}]],
 'list':[['line',{x1:8,y1:6,x2:21,y2:6}],['line',{x1:8,y1:12,x2:21,y2:12}],['line',{x1:8,y1:18,x2:21,y2:18}],['line',{x1:3,y1:6,x2:3,y2:6}],['line',{x1:3,y1:12,x2:3,y2:12}],['line',{x1:3,y1:18,x2:3,y2:18}]],
 'git-branch':[['line',{x1:6,y1:3,x2:6,y2:15}],['circle',{cx:6,cy:3,r:2}],['circle',{cx:6,cy:17,r:2}],['circle',{cx:18,cy:6,r:2}],['path',{d:'M18 8a6 6 0 0 1-6 6H6'}]],
 'activity':[['polyline',{points:'22 12 18 12 15 21 9 3 6 12 2 12'}]],
 'terminal':[['polyline',{points:'4 17 10 11 4 5'}],['line',{x1:12,y1:19,x2:20,y2:19}]],
 'arrow-up':[['line',{x1:12,y1:19,x2:12,y2:5}],['polyline',{points:'5 12 12 5 19 12'}]]
 };
 const NS='http://www.w3.org/2000/svg';
 window.feather={replace:()=>{
   document.querySelectorAll('[data-feather]').forEach(el=>{
     const key=el.getAttribute('data-feather');if(!shapes[key])return;
     const svg=document.createElementNS(NS,'svg');
     for(const [k,v] of Object.entries({xmlns:NS,width:24,height:24,viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':2,'stroke-linecap':'round','stroke-linejoin':'round',class:el.getAttribute('class')||''}))svg.setAttribute(k,v);
     svg.setAttribute('aria-hidden','true');
     shapes[key].forEach(([name,attrs])=>{const part=document.createElementNS(NS,name);for(const [key,val] of Object.entries(attrs))part.setAttribute(key,String(val));svg.appendChild(part);});
     el.replaceWith(svg);
   });
 }};
})();
