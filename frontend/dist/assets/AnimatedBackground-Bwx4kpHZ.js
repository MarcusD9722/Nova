import{r as d,j as o}from"./index-D0eijIkW.js";import{V as I,a as f,C as q,u as j,B as G,b as O,A as H}from"./react-three-fiber.esm-CNNOyQEe.js";function J({zPlane:n=0,outRef:e}){return j(({camera:t,size:s})=>{if(!e?.current)return;const{ndc:c,world:i}=e.current,a=e.current._clientX??s.width/2,v=e.current._clientY??s.height/2,l=a/s.width*2-1,h=-(v/s.height)*2+1;c.set(l,h);const m=new f(l,h,.5).clone().unproject(t).sub(t.position).normalize(),z=(n-t.position.z)/m.z;i.copy(t.position).add(m.multiplyScalar(z))}),d.useEffect(()=>{if(!e?.current)return;const t=(i,a)=>{e.current._clientX=i,e.current._clientY=a},s=i=>t(i.clientX,i.clientY),c=i=>{i.touches&&i.touches[0]&&t(i.touches[0].clientX,i.touches[0].clientY)};return window.addEventListener("mousemove",s,{passive:!0}),window.addEventListener("touchmove",c,{passive:!0}),()=>{window.removeEventListener("mousemove",s),window.removeEventListener("touchmove",c)}},[e]),null}function K({cursorRef:n,shockRef:e}){return d.useEffect(()=>{if(!e?.current)return;const t=()=>{const s=n?.current?.world??new f;e.current.center.copy(s),e.current.time=performance.now()/1e3,e.current.active=!0};return window.addEventListener("mousedown",t,{passive:!0}),window.addEventListener("touchstart",t,{passive:!0}),()=>{window.removeEventListener("mousedown",t),window.removeEventListener("touchstart",t)}},[n,e]),null}function N({cursorRef:n,radius:e=6,color:t="#22d3ee",opacity:s=.35}){const c=d.useRef(),i=d.useMemo(()=>{const a=[];for(let v=0;v<=64;v++){const l=v/64*Math.PI*2;a.push(new f(Math.cos(l)*e,Math.sin(l)*e,0))}return new G().setFromPoints(a)},[e]);return j(()=>{if(!c.current||!n?.current)return;const a=n.current.world;c.current.position.set(a.x,a.y,0)}),o.jsxs("line",{ref:c,renderOrder:10,children:[o.jsx("primitive",{object:i,attach:"geometry"}),o.jsx("lineBasicMaterial",{color:t,transparent:!0,opacity:s,depthWrite:!1})]})}function g({count:n=900,radius:e=14,color:t="#48eaff",speed:s=.45,swirl:c=.1,wobble:i=.8,sizeMin:a=.1,sizeMax:v=.26,interactionMode:l="repel",cursorRadius:h=6.5,cursorStrength:y=3,cursorRef:b,shockRef:m,shockSpeed:z=5,shockBaseRadius:_=0,shockWidth:R=1,shockStrength:T=1.8,shockDecay:E=2}){const{positions:P,sizes:B,seeds:L}=d.useMemo(()=>{const p=new Float32Array(n*3),x=new Float32Array(n),u=new Float32Array(n);for(let r=0;r<n;r++){const w=Math.random(),V=Math.random(),A=2*Math.PI*w,M=Math.acos(2*V-1),k=e*(.75+.25*Math.random()),X=k*Math.sin(M)*Math.cos(A),Y=k*Math.sin(M)*Math.sin(A),D=k*Math.cos(M);p[r*3+0]=X,p[r*3+1]=Y,p[r*3+2]=D,x[r]=a+Math.random()*(v-a),u[r]=Math.random()*1e3+r*.137}return{positions:p,sizes:x,seeds:u}},[n,e,a,v]),W=`
    precision highp float;
    attribute float aSize;
    attribute float aSeed;

    uniform float uTime;
    uniform float uSpeed;
    uniform float uSwirl;
    uniform float uWobble;
    uniform float uRadius;

    uniform vec3  uCursor;
    uniform float uCursorRadius;
    uniform float uCursorStrength;
    uniform int   uMode; // 0=repel,1=attract,2=orbit,3=highlight

    uniform vec3  uShockCenter;
    uniform float uShockTime;
    uniform float uShockActive;
    uniform float uShockSpeed;
    uniform float uShockBaseRadius;
    uniform float uShockWidth;
    uniform float uShockStrength;
    uniform float uShockDecay;

    // 3D simplex noise (compact)
    vec3 mod289(vec3 x){return x - floor(x*(1.0/289.0))*289.0;}
    vec4 mod289(vec4 x){return x - floor(x*(1.0/289.0))*289.0;}
    vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}
    vec4 taylorInvSqrt(vec4 r){return 1.7928429 - 0.8537347*r;}
    float snoise(vec3 v){
      const vec2 C=vec2(1.0/6.0,1.0/3.0);
      vec3 i=floor(v+dot(v,C.yyy)); vec3 x0=v-i+dot(i,C.xxx);
      vec3 g=step(x0.yzx,x0.xyz); vec3 l=1.0-g;
      vec3 i1=min(g.xyz,l.zxy); vec3 i2=max(g.xyz,l.zxy);
      vec3 x1=x0-i1+C.xxx, x2=x0-i2+2.0*C.xxx, x3=x0-1.0+3.0*C.xxx;
      i=mod289(i);
      vec4 p=permute(permute(permute(
        i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));
      float n_=1.0/7.0; vec3 ns=n_*vec3(1.0,2.0,3.0)-vec3(0.0);
      vec4 j=p-49.0*floor(p*(1.0/7.0)*(1.0/7.0));
      vec4 x_=floor(j*(1.0/7.0)), y_=floor(j-7.0*x_);
      vec4 x=x_*(1.0/6.0)+(1.0/3.0), y=y_*(1.0/6.0)+(1.0/3.0);
      vec4 h=1.0-abs(x)-abs(y);
      vec4 b0=vec4(x.xy,y.xy), b1=vec4(x.zw,y.zw);
      vec4 s0=floor(b0)*2.0+1.0, s1=floor(b1)*2.0+1.0;
      vec4 sh=-step(h,vec4(0.0));
      vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy, a1=b1.xzyw+s1.xzyw*sh.zzww;
      vec3 p0=vec3(a0.xy,h.x), p1=vec3(a1.xy,h.y), p2=vec3(a0.zw,h.z), p3=vec3(a1.zw,h.w);
      vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
      p0*=norm.x; p1*=norm.y; p2*=norm.z; p3*=norm.w;
      vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0); m*=m;
      return 42.0*dot(m*m, vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
    }

    varying float vAlpha;

    void main() {
      vec3 pos = position;

      // baseline swirl (slower)
      float ang = uSwirl * (uTime * uSpeed * 0.6 + aSeed * 0.01);
      float s = sin(ang), c = cos(ang);
      pos = vec3(c*pos.x + s*pos.z, pos.y, -s*pos.x + c*pos.z);

      // wobble (subtle)
      float n1 = snoise(pos * 0.12 + vec3(0.0, uTime * 0.12 * uSpeed, aSeed));
      float n2 = snoise(pos * 0.21 + vec3(uTime * 0.06 * uSpeed, aSeed, 0.0));
      vec3 wob = normalize(vec3(n1, n2, n1 - n2 + 0.2)) * uWobble;
      pos += wob;

      // cursor field
      vec3 toC = uCursor - pos;
      float d = length(toC);
      float influence = 1.0 - smoothstep(0.0, uCursorRadius, d);
      influence *= influence;
      float nearBoost = 1.0 / (0.22 + d * 0.8);
      float force = uCursorStrength * nearBoost;

      if (uMode == 0) { pos -= normalize(toC) * force * influence; }
      else if (uMode == 1) { pos += normalize(toC) * force * influence; }
      else if (uMode == 2) {
        vec3 perp = normalize(vec3(-toC.z, 0.0, toC.x));
        pos += perp * (force * 0.9) * influence;
        pos += normalize(toC) * (force * 0.15) * influence;
      }

      // shockwave ring
      if (uShockActive > 0.5) {
        float age = max(0.0, uTime - uShockTime);
        float ringR = uShockBaseRadius + age * uShockSpeed;
        vec3  toS = pos - uShockCenter;
        float distS = length(toS);
        float band = 1.0 - smoothstep(0.0, uShockWidth, abs(distS - ringR));
        float amp = uShockStrength * band * exp(-uShockDecay * age);
        if (distS > 0.0001) { pos += normalize(toS) * amp; }
      }

      // soft boundary
      float len = length(pos);
      if (len > uRadius * 1.15) pos *= (uRadius * 1.15) / len;

      // size + fade
      vec4 mvPosition = modelViewMatrix * vec4(pos, 1.0);
      float dist = length(mvPosition.xyz);
      float baseSize = aSize * (400.0 / dist);
      float pulse = 0.85 + 0.3 * sin(uTime * (1.0 * uSpeed) + aSeed);
      gl_PointSize = baseSize * pulse;

      vAlpha = smoothstep(uRadius * 1.25, uRadius * 0.2, len);
      if (uMode == 3) { vAlpha += 0.6 * influence; } // highlight

      gl_Position = projectionMatrix * mvPosition;
    }
  `,F=`
    precision highp float;
    uniform vec3  uColor;
    varying float vAlpha;
    void main() {
      vec2 uv = gl_PointCoord - vec2(0.5);
      float d = length(uv);
      float a = smoothstep(0.5, 0.05, d);
      gl_FragColor = vec4(uColor, a * 0.85 * vAlpha);
    }
  `,C=d.useRef(),S=d.useRef();return j(({clock:p})=>{const x=p.getElapsedTime();if(S.current&&(S.current.rotation.y+=.001,S.current.rotation.x+=3e-4),!C.current)return;const u=C.current.uniforms;if(u.uTime.value=x,b?.current){const r=b.current.world;u.uCursor.value.set(r.x,r.y,r.z)}if(u.uCursorRadius.value=h,u.uCursorStrength.value=y,u.uMode.value=l==="repel"?0:l==="attract"?1:l==="orbit"?2:3,m?.current){const r=m.current;u.uShockCenter.value.copy(r.center),u.uShockTime.value=r.time;const w=r.active&&x-r.time<2.5?1:0;u.uShockActive.value=w,!w&&r.active&&(r.active=!1)}}),o.jsx("group",{ref:S,frustumCulled:!1,children:o.jsxs("points",{renderOrder:1,children:[o.jsxs("bufferGeometry",{children:[o.jsx("bufferAttribute",{attach:"attributes-position",array:P,count:n,itemSize:3}),o.jsx("bufferAttribute",{attach:"attributes-aSize",array:B,count:n,itemSize:1}),o.jsx("bufferAttribute",{attach:"attributes-aSeed",array:L,count:n,itemSize:1})]}),o.jsx("shaderMaterial",{ref:C,vertexShader:W,fragmentShader:F,depthWrite:!1,depthTest:!0,transparent:!0,blending:H,uniforms:{uTime:{value:0},uSpeed:{value:s},uSwirl:{value:c},uWobble:{value:i},uRadius:{value:e},uColor:{value:new O(t)},uCursor:{value:new f(0,0,0)},uCursorRadius:{value:h},uCursorStrength:{value:y},uMode:{value:0},uShockCenter:{value:new f(0,0,0)},uShockTime:{value:0},uShockActive:{value:0},uShockSpeed:{value:z},uShockBaseRadius:{value:_},uShockWidth:{value:R},uShockStrength:{value:T},uShockDecay:{value:E}}})]})})}function Z({showDebugRing:n=!1}){const e=d.useRef({world:new f,ndc:new I(0,0),_clientX:void 0,_clientY:void 0}),t=d.useRef({active:!1,time:0,center:new f});return o.jsx("div",{style:{position:"absolute",inset:0,width:"100vw",height:"100vh",zIndex:0,pointerEvents:"none",background:"radial-gradient(ellipse at 60% 40%, #171a1e 70%, #080c11 100%)"},children:o.jsxs(q,{camera:{position:[0,0,32],fov:55},dpr:[1,2],gl:{antialias:!0,alpha:!0,depth:!0,powerPreference:"high-performance"},frameloop:"always",children:[o.jsx("color",{attach:"background",args:["#070b14"]}),o.jsx("ambientLight",{intensity:1}),o.jsx(J,{zPlane:0,outRef:e}),o.jsx(K,{cursorRef:e,shockRef:t}),n&&o.jsx(N,{cursorRef:e,radius:6}),o.jsx(g,{count:1600,radius:28,color:"#1e90ff",speed:.2,swirl:.05,wobble:.6,sizeMin:.08,sizeMax:.18,interactionMode:"attract",cursorRef:e,cursorRadius:9,cursorStrength:1,shockRef:t,shockStrength:.9}),o.jsx(g,{count:1200,radius:20,color:"#ffe872",speed:.22,swirl:.06,wobble:.65,sizeMin:.1,sizeMax:.22,interactionMode:"attract",cursorRef:e,cursorRadius:7.5,cursorStrength:1.4,shockRef:t,shockStrength:1.1}),o.jsx(g,{count:2400,radius:14,color:"#48eaff",speed:.35,swirl:.09,wobble:.85,sizeMin:.12,sizeMax:.26,interactionMode:"repel",cursorRef:e,cursorRadius:7,cursorStrength:3,shockRef:t,shockStrength:1.6}),o.jsx(g,{count:800,radius:9,color:"#47fff2",speed:.5,swirl:.12,wobble:1,sizeMin:.16,sizeMax:.34,interactionMode:"orbit",cursorRef:e,cursorRadius:6,cursorStrength:3.4,shockRef:t,shockStrength:1.8})]})})}export{Z as default};
