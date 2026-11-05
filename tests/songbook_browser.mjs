// Run with an installed Playwright module and Chromium; see README development.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'kilix-songbook-browser-'));
let browser;
const server = http.createServer(async (request, response) => {
  try {
    const name = new URL(request.url, 'http://localhost').pathname.slice(1);
    if (!['index.html','no-audio.html','large-corrections.html','mix.mp3','guitar.mp3'].includes(name)) {
      response.writeHead(404); response.end(); return;
    }
    const bytes = await fs.readFile(path.join(directory, name));
    const mime = name.endsWith('.html') ? 'text/html; charset=utf-8' : 'audio/mpeg';
    const range = /^bytes=(\d+)-(\d*)$/.exec(request.headers.range || '');
    const start = range ? Number(range[1]) : 0;
    const end = range?.[2] ? Math.min(Number(range[2]), bytes.length - 1) : bytes.length - 1;
    const headers = {'Content-Type':mime,'Accept-Ranges':'bytes','Content-Length':end-start+1};
    if(range) headers['Content-Range'] = `bytes ${start}-${end}/${bytes.length}`;
    response.writeHead(range ? 206 : 200,headers); response.end(bytes.subarray(start,end+1));
  } catch { response.writeHead(500); response.end(); }
});
try {
  execFileSync(process.env.PYTHON || path.join(repo,'.venv/bin/python'),
    [path.join(repo,'tests/browser_fixture.py'),directory],{cwd:repo,stdio:'inherit'});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin=`http://127.0.0.1:${server.address().port}`;
  browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH || undefined,args:['--disable-gpu']});
  const context=await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true});
  const page=await context.newPage(), errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  let delayAudio=false;
  await page.route('**/*.mp3',async route=>{
    if(delayAudio)await new Promise(resolve=>setTimeout(resolve,350));
    await route.continue();
  });
  const ready=()=>page.waitForFunction(()=>document.getElementById('audio').readyState>=2);
  const state=()=>page.evaluate(()=>{const a=document.getElementById('audio');return {at:a.currentTime,paused:a.paused,rate:a.playbackRate,src:a.currentSrc};});
  await page.goto(`${origin}/index.html`);await ready();
  await page.evaluate(()=>document.getElementById('audio').muted=true);
  assert.equal(await page.title(),'Another Song · Guitar Songbook');
  assert.equal(await page.locator('.song-facts strong').textContent(),'D A D G B e');
  assert.equal(await page.locator('[data-event-id]').count(),4);
  assert.equal(await page.locator('.lyric-text:not(.instrumental)').count(),2);
  const ids=await page.locator('[id]').evaluateAll(nodes=>nodes.map(n=>n.id));
  assert.equal(new Set(ids).size,ids.length);

  await page.evaluate(()=>{const a=document.getElementById('audio');a.currentTime=1.1;a.dispatchEvent(new Event('timeupdate'));});
  assert.deepEqual(await page.locator('.tab-event.current').evaluateAll(nodes=>nodes.map(n=>n.dataset.eventId)),['0','1']);
  await page.evaluate(()=>{const a=document.getElementById('audio');a.currentTime=2.6;a.dispatchEvent(new Event('timeupdate'));});
  assert.deepEqual(await page.locator('.tab-event.current').evaluateAll(nodes=>nodes.map(n=>n.dataset.eventId)),['0']);
  console.log('PASS sustained notes stay highlighted across other notes and lyric rows');

  await page.locator('#follow').uncheck();
  await page.locator('#play').click();
  await page.waitForFunction(()=>!document.getElementById('audio').paused);
  delayAudio=true;
  await page.evaluate(()=>{
    const track=document.getElementById('track');
    for(const value of ['guitar','mix','guitar']) {track.value=value;track.dispatchEvent(new Event('change'));}
    const seek=document.getElementById('seek');seek.value=2;seek.dispatchEvent(new Event('input'));
    const speed=document.getElementById('speed');
    speed.value='0.65';speed.dispatchEvent(new Event('change'));
    document.getElementById('play').click();
  });
  await ready();
  let audio=await state();assert.ok(Math.abs(audio.at-2)<.1);assert.ok(audio.paused);assert.equal(audio.rate,.65);assert.ok(audio.src.endsWith('/guitar.mp3'));
  await page.locator('#play').click();
  await page.waitForFunction(()=>!document.getElementById('audio').paused);
  await page.evaluate(()=>{const t=document.getElementById('track');for(const value of ['mix','guitar','mix']){t.value=value;t.dispatchEvent(new Event('change'));}});
  await page.waitForFunction(()=>document.getElementById('audio').readyState>=2&&!document.getElementById('audio').paused);
  audio=await state();assert.ok(audio.at>=2&&audio.at<4);assert.equal(audio.rate,.65);
  await page.locator('#play').click();delayAudio=false;
  await page.evaluate(()=>{const play=document.getElementById('play');play.click();play.click();play.click();});
  await page.waitForFunction(()=>!document.getElementById('audio').paused&&document.getElementById('play').getAttribute('aria-label')==='Pause song');
  await page.waitForTimeout(200);
  assert.equal(await page.locator('#audio-status').textContent(),'');
  await page.locator('#play').click();
  console.log('PASS rapid track changes preserve seeks, pause/play intent, and speed during loading');

  const phrase=await page.evaluate(()=>window.songbook.rows[0]);
  const phraseButton=page.locator(`[data-loop-phrase="${phrase.id}"]`);
  await phraseButton.click();
  assert.equal(await phraseButton.getAttribute('aria-pressed'),'true');
  await page.locator('#play').click();
  await page.waitForFunction(()=>!document.getElementById('audio').paused);
  await page.evaluate(end=>{const a=document.getElementById('audio');a.currentTime=end+.1;a.dispatchEvent(new Event('timeupdate'));},phrase.end);
  audio=await state();assert.ok(audio.at>=phrase.start&&audio.at<phrase.start+.5);
  await page.locator('#track').selectOption('guitar');
  await page.waitForFunction(()=>document.getElementById('audio').readyState>=2&&!document.getElementById('audio').paused);
  assert.equal(await phraseButton.getAttribute('aria-pressed'),'true');
  await page.locator('#play').click();
  await page.locator('#loop').click();
  assert.equal(await phraseButton.getAttribute('aria-pressed'),'false');
  await phraseButton.click();
  await page.locator('[data-event-id="2"]').click();
  await page.waitForFunction(()=>!document.getElementById('audio').paused);
  assert.equal(await phraseButton.getAttribute('aria-pressed'),'false');
  assert.equal(await page.locator('#loop').getAttribute('aria-pressed'),'false');
  await page.locator('#play').click();
  console.log('PASS phrase loops wrap, survive track changes, stop from the player, and clear when seeking away');

  await page.locator('#edit-sections').click();await page.locator('#add-section').click();
  await page.locator('[data-section-label="1"]').fill('Chorus');
  await page.locator('[data-section-kind="1"]').selectOption('chorus');
  await page.locator('[data-section-start="1"]').fill('0:03');
  await page.locator('#edit-form button[type=submit]').click();
  assert.equal(await page.locator('.song-section').count(),2);
  assert.equal(await page.locator('[data-event-id]').count(),4);
  await page.reload();await ready();
  assert.equal(await page.locator('.song-section').count(),2);
  await page.locator('.section-kind-chorus [data-loop-section]').click();
  await page.locator('#edit-sections').click();
  await page.locator('[data-section-start="1"]').fill('0:04');
  await page.locator('#edit-form button[type=submit]').click();
  assert.ok(Math.abs((await state()).at-4)<.1);
  assert.equal(await page.locator('.section-kind-chorus [data-loop-section]').getAttribute('aria-pressed'),'true');
  await page.locator('#play').click();
  await page.waitForFunction(()=>!document.getElementById('audio').paused);
  assert.ok((await state()).at>=4);
  await page.locator('#play').click();
  await page.locator('#edit-sections').click();
  await page.locator('.section-editor-row').last().getByRole('button',{name:'Remove'}).click();
  await page.locator('#edit-form button[type=submit]').click();
  assert.equal(await page.locator('.song-section').count(),1);
  assert.equal(await page.locator('#loop').getAttribute('aria-pressed'),'false');
  assert.equal(await page.locator('[data-event-id]').count(),4);
  console.log('PASS section edits keep active loops within their boundaries and preserve every note');

  await page.locator('.edit-lyric').first().click();await page.locator('#lyric-editor').fill('Correction <test>');
  await page.locator('#edit-form button[type=submit]').click();
  const downloadPromise=page.waitForEvent('download');await page.locator('#export-edits').click();
  const download=await downloadPromise;
  const corrections=JSON.parse(await fs.readFile(await download.path(),'utf8'));
  assert.equal(download.suggestedFilename(),'Another-Song-corrections.json');
  corrections.revision='different-transcript';
  const stale=path.join(directory,'stale.json');await fs.writeFile(stale,JSON.stringify(corrections));
  await page.locator('#import-edits').setInputFiles(stale);
  await page.waitForFunction(()=>document.getElementById('save-status').textContent.includes('different transcript'));
  assert.equal(await page.locator('.lyric-text').first().textContent(),'Correction <test>');
  assert.equal(await page.locator('.lyric-text test').count(),0);
  console.log('PASS stale corrections are rejected and lyric text cannot inject HTML');

  await page.goto(`${origin}/large-corrections.html`);
  const large=await page.evaluate(()=>({schema:'songbook.corrections/v1',id:window.songbook.data.id,
    revision:window.songbook.data.revision,
    lyrics:Object.fromEntries(window.songbook.data.cues.map(c=>[c.id,'練習'.repeat(1000)]))}));
  const buffer=Buffer.from(JSON.stringify(large,null,2));assert.ok(buffer.length>100000);
  await page.locator('#import-edits').setInputFiles({name:'large.json',mimeType:'application/json',buffer});
  await page.waitForFunction(()=>document.querySelector('.lyric-text').textContent==='練習'.repeat(1000));
  const largeDownloadPromise=page.waitForEvent('download');await page.locator('#export-edits').click();
  const largeDownload=await largeDownloadPromise;
  const exported=await fs.readFile(await largeDownload.path());assert.ok(exported.length>100000);
  assert.deepEqual(JSON.parse(exported).lyrics,large.lyrics);
  page.once('dialog',dialog=>dialog.accept());await page.locator('#reset-edits').click();
  assert.equal(await page.locator('.lyric-text').first().textContent(),'Practice line 0');
  await page.locator('#import-edits').setInputFiles({name:'roundtrip.json',mimeType:'application/json',buffer:exported});
  await page.waitForFunction(()=>document.querySelector('.lyric-text').textContent==='練習'.repeat(1000));
  await page.reload();
  assert.equal(await page.locator('.lyric-text').last().textContent(),'練習'.repeat(1000));
  await page.locator('#import-edits').setInputFiles({name:'oversized.json',mimeType:'application/json',buffer:Buffer.alloc(1000000,32)});
  await page.waitForFunction(()=>document.getElementById('save-status').textContent.includes('too large'));
  assert.equal(await page.locator('.lyric-text').first().textContent(),'練習'.repeat(1000));
  console.log('PASS large Unicode corrections import, export, reset, reimport, and persist');

  await page.goto(`${origin}/no-audio.html`);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.equal(await page.locator('.transport').isVisible(),false);
  assert.equal(await page.locator('.section-actions').isVisible(),false);
  assert.equal(await page.locator('[data-loop-phrase]').count(),0);
  assert.equal(await page.locator('[data-event-id]').count(),4);
  await page.locator('[data-event-id]').first().click();
  await page.locator('#edit-sections').click();await page.locator('#cancel-editor').click();
  await page.pdf({path:path.join(directory,'chart.pdf'),preferCSSPageSize:true});
  assert.deepEqual(errors,[]);
  console.log('PASS small screens, charts without audio, printing, and browser error checks');
} finally {
  if(browser)await browser.close();
  server.closeAllConnections();await new Promise(resolve=>server.close(resolve));
  await fs.rm(directory,{recursive:true,force:true});
}
