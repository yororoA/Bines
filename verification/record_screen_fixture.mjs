/** Record only a new, isolated headless browser showing our own local fixture.
 * No user profile, desktop capture, external page, or model call is involved.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const root=path.dirname(here);
const require=createRequire(path.join(root,'.tools/agent-browser/package.json'));
const {chromium}=require('playwright-core');
const args=Object.fromEntries(process.argv.slice(2).map(x=>{const i=x.indexOf('=');return [x.slice(2,i),x.slice(i+1)]}));
const out=path.resolve(args.out||path.join(root,'docs/tests/see-now-screen/evidence/browser-recording'));
const fps=Number(args.fps||6),duration=Number(args.duration||36);
if(!(fps>0&&fps<=30&&duration>0&&duration<=120))throw Error('fps/duration outside benchmark bounds');
await fs.mkdir(path.join(out,'frames'),{recursive:true});
const fixture=await fs.readFile(path.join(here,'fixtures/screen_replay.html'));
const server=http.createServer((req,res)=>{res.writeHead(200,{'content-type':'text/html; charset=utf-8','cache-control':'no-store'});res.end(fixture)});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const url=`http://127.0.0.1:${server.address().port}/`;
let browser;
try{
 browser=await chromium.launch({headless:true,...(args.executable?{executablePath:args.executable}:{})});
 const context=await browser.newContext({viewport:{width:1280,height:720},deviceScaleFactor:2});
 const page=await context.newPage();
 await page.goto(url,{waitUntil:'load'});
 await page.getByRole('button',{name:'开始演示'}).click();
 const epoch=Date.now()/1000;
 const frames=[];
 const start=performance.now();
 for(let index=0;performance.now()-start<duration*1000;index++){
  const due=start+index*1000/fps;
  if(due>performance.now())await new Promise(r=>setTimeout(r,due-performance.now()));
  const before=await page.evaluate(()=>({phase:window.captureEvidence.phase,t:(performance.now()-window.captureEvidence.startedAt)/1000}));
  const name=`frames/frame-${String(index).padStart(5,'0')}.png`;
  await page.screenshot({path:path.join(out,name),type:'png',animations:'allow'});
  const after=await page.evaluate(()=>({phase:window.captureEvidence.phase,t:(performance.now()-window.captureEvidence.startedAt)/1000}));
  frames.push({index,path:name,t:(before.t+after.t)/2,before:before.t,after:after.t,phase:after.phase,transitionDuringCapture:before.phase!==after.phase});
 }
 const evidence=await page.evaluate(()=>window.captureEvidence);
 const manifest={schema:'bines-screen-recording/v1',source:'isolated-headless-browser-screenshots',syntheticFrames:false,fixture:path.relative(root,path.join(here,'fixtures/screen_replay.html')),viewport:{width:1280,height:720,dpr:2},pixelSize:{width:2560,height:1440},requestedFps:fps,epoch,durationSeconds:frames.at(-1).t,browserVersion:browser.version(),frameCount:frames.length,frames,events:evidence.events};
 await fs.writeFile(path.join(out,'manifest.json'),JSON.stringify(manifest,null,2));
 await fs.copyFile(path.join(here,'fixtures/screen_replay.html'),path.join(out,'fixture.html'));
 // Keep actual screenshot times, including static waiting, in the review video.
 const concat=frames.map((f,i)=>`file '${path.resolve(out,f.path).replaceAll("'","'\\''")}'\nduration ${i+1<frames.length?frames[i+1].t-f.t:1/fps}`).join('\n');
 await fs.writeFile(path.join(out,'frames.ffconcat'),'ffconcat version 1.0\n'+concat+'\n');
 console.log(JSON.stringify({out,frameCount:frames.length,durationSeconds:manifest.durationSeconds,browserVersion:manifest.browserVersion}));
}finally{if(browser)await browser.close();await new Promise(r=>server.close(r))}
