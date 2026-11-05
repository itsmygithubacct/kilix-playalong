(() => {
  'use strict';
  const base = JSON.parse(document.getElementById('song-data').textContent);
  const $ = id => document.getElementById(id);
  const audio = $('audio');
  const legacyKey = `songbook:${base.id}:v1`;
  const storageKey = `songbook:${base.id}:${base.revision}:v1`;
  const hasAudio = Boolean(Object.keys(base.audio).length);
  const reservedIds = new Set([...document.querySelectorAll('[id]')].map(n => n.id));
  for(const id of ['lyric-editor','section-editor-list','add-section'])reservedIds.add(id);
  const kinds = ['intro', 'verse', 'chorus', 'bridge', 'interlude', 'outro', 'section'];
  let saved = {}, rows = [], sections = [], currentRow = null, currentEvents = new Set();
  let currentSectionId = base.sections[0].id, loopTarget = null, editorMode = null;
  let editedCueId = null, tabSize = 13, trackChange = 0, pendingTrack = null;
  let selectedTime = 0, wantPlaying = false, frame = null, playRequest = 0;
  const rowNodes = new Map(), eventNodes = new Map(), cueMap = new Map();
  const pitches = ['C','C♯','D','D♯','E','F','F♯','G','G♯','A','A♯','B'];
  // Bound imports by the edits this page can export, including UTF-8 and JSON
  // escaping of every allowed lyric/section character, plus formatting overhead.
  const maxCorrectionsBytes = 4096 + base.cues.length * 12288 + 200 * 2048;

  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  function time(value, precise = false) {
    const raw = Math.max(0, Number(value) || 0);
    const seconds = precise ? Math.round(raw * 100) / 100 : Math.floor(raw);
    const minutes = Math.floor(seconds / 60);
    return `${minutes}:${(seconds % 60).toFixed(precise ? 2 : 0).padStart(precise ? 5 : 2, '0')}`;
  }
  function parseTime(value) {
    const text = String(value).trim();
    if (!/^(?:\d+:)?\d+(?:\.\d+)?$/.test(text)) return NaN;
    const parts = text.split(':').map(Number);
    if (parts.length === 2 && parts[1] >= 60) return NaN;
    return parts.length === 2 ? parts[0] * 60 + parts[1] : parts[0];
  }
  function validateCorrections(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('This is not a corrections file.');
    if (value.id && value.id !== base.id) throw Error('These corrections belong to a different song.');
    if (value.revision && value.revision !== base.revision) throw Error('These corrections belong to a different transcript or arrangement. Reapply the edits after reviewing the changed lines.');
    const clean = {id: base.id, revision: base.revision, lyrics: {}};
    if (value.lyrics !== undefined) {
      if (!value.lyrics || typeof value.lyrics !== 'object' || Array.isArray(value.lyrics)) throw Error('The lyric corrections are invalid.');
      for (const [id, text] of Object.entries(value.lyrics)) {
        if (!base.cues.some(c => c.id === id) || typeof text !== 'string' || text.length > 2000) throw Error('A lyric correction is invalid.');
        clean.lyrics[id] = text;
      }
    }
    if (value.sections !== undefined) {
      if (!Array.isArray(value.sections) || value.sections.length < 1 || value.sections.length > 200) throw Error('Use between 1 and 200 sections.');
      const ids = new Set();
      clean.sections = value.sections.map((s, i) => {
        if (!s || typeof s.id !== 'string' || !/^[a-z][a-z0-9-]{0,79}$/.test(s.id) || s.id.startsWith('row-') || ids.has(s.id) || reservedIds.has(s.id) || reservedIds.has(`${s.id}-title`)) throw Error('Section IDs must be unique names.');
        ids.add(s.id);
        if (typeof s.label !== 'string' || !s.label.trim() || s.label.length > 80) throw Error('A section name is invalid.');
        if (!kinds.includes(s.kind)) throw Error('Choose a valid section type.');
        if (!Number.isFinite(s.start) || s.start < 0 || s.start >= base.duration) throw Error('Section times must fall inside the song.');
        if (i === 0 && s.start !== 0) throw Error('The first section must start at 0:00.');
        if (i > 0 && s.start <= value.sections[i - 1].start) throw Error('Section markers must be in increasing time order.');
        return {id:s.id, kind:s.kind, label:s.label.trim(), start:s.start};
      });
      if (clean.sections.some(s => ids.has(`${s.id}-title`))) throw Error('Section IDs collide with section headings.');
    }
    return clean;
  }
  try {
    const stored = localStorage.getItem(storageKey);
    if (stored) saved = validateCorrections(JSON.parse(stored));
    const previous = localStorage.getItem(legacyKey);
    if (previous) {
      $('save-status').textContent = 'Older corrections are preserved separately. Review their lines before reapplying them to this transcript.';
      $('save-status').after(makeButton('Download older corrections', 'button quiet small', () => downloadCorrections(JSON.parse(previous))));
    }
  } catch (_) {
    $('save-status').textContent = 'Browser storage is unavailable or unreadable. Download corrections to keep them.';
  }
  function persist() {
    try {
      localStorage.setItem(storageKey, JSON.stringify(saved));
      $('save-status').textContent = 'Corrections saved in this browser.';
    } catch (_) {
      $('save-status').textContent = 'Changes are active. Download corrections to keep them.';
    }
  }
  function lyricText(cue) { return saved.lyrics?.[cue.id] ?? cue.text; }
  function activeSection(at) { return sections.find(s => at >= s.start && at < s.end) || sections[sections.length - 1]; }
  function activeLoop() {
    if (loopTarget?.kind === 'section') return sections.find(s => s.id === loopTarget.id);
    if (loopTarget?.kind === 'phrase') {
      const row = rows.find(r => r.id === loopTarget.id);
      if (row) return {...row, label:`Phrase ${time(row.start, true)}`};
    }
    return null;
  }

  function arrange() {
    sections = (saved.sections || base.sections).map((s, i, all) => ({...s, end:all[i + 1]?.start ?? base.duration}));
    rows = [];
    for (const section of sections) {
      let cursor = section.start;
      const relevant = base.cues.filter(c => c.end > section.start && c.start < section.end);
      const append = (start, end, cue = null) => {
        if (end - start < 0.00001) return;
        rows.push({id:`row-${rows.length}`, sectionId:section.id, kind:section.kind, start, end, cue,
          events:base.events.filter(e => e.start >= start && e.start < end)});
      };
      const gap = (start, end) => {
        // Instrumental passages have short, playable lines too.
        for (let t = start; t < end - 0.00001; t += 6) append(t, Math.min(t + 6, end));
      };
      relevant.forEach((cue, i) => {
        const start = Math.max(section.start, cue.start, cursor);
        if (start > cursor + 1.2) gap(cursor, start);
        const lineStart = start > cursor + 1.2 ? start : cursor;
        let end = Math.min(section.end, cue.end);
        const nextStart = Math.min(section.end, relevant[i + 1]?.start ?? section.end);
        if (nextStart >= end && nextStart - end <= 1.2) end = nextStart;
        append(lineStart, end, cue);
        cursor = Math.max(cursor, end);
      });
      gap(cursor, section.end);
    }
    const ids = rows.flatMap(r => r.events.map(e => e.id));
    if (ids.length !== base.events.length || new Set(ids).size !== base.events.length) throw Error('The section markers did not preserve every tab event.');
  }
  function makeButton(text, cls, action, title) {
    const button = node('button', cls, text);
    button.type = 'button';
    if (title) button.title = title;
    button.addEventListener('click', action);
    return button;
  }
  function seekTo(at, play = false, scroll = false) {
    const bounded = Math.max(0, Math.min(base.duration, at));
    const loop = activeLoop();
    if (loop && (bounded < loop.start || bounded >= loop.end)) loopTarget = null;
    selectedTime = bounded;
    if (pendingTrack) pendingTrack.at = bounded;
    else if (hasAudio) audio.currentTime = bounded;
    update(bounded, scroll);
    refreshLoop();
    if (play) startPlaying();
  }
  async function startPlaying() {
    if (!hasAudio) return;
    const request = ++playRequest;
    wantPlaying = true; refreshPlay();
    const loop = activeLoop();
    if (loop && (selectedTime < loop.start || selectedTime >= loop.end)) seekTo(loop.start);
    if (pendingTrack) return;
    const change = trackChange;
    try { await audio.play(); if (change === trackChange && request === playRequest) $('audio-status').textContent = ''; }
    catch (error) {
      if (change !== trackChange || request !== playRequest || !wantPlaying) return;
      wantPlaying = false; refreshPlay();
      $('audio-status').textContent = 'Audio could not start. Keep the exported MP3 files beside this page, then press Play.';
    }
  }
  function pausePlaying() { playRequest++; wantPlaying = false; audio.pause(); refreshPlay(); }
  function refreshPlay() {
    $('play').textContent = wantPlaying ? 'Ⅱ' : '▶';
    $('play').setAttribute('aria-label', wantPlaying ? 'Pause song' : 'Play song');
  }
  function togglePlay() {
    if (!wantPlaying) {
      if (selectedTime >= base.duration - 0.1) seekTo(activeLoop()?.start ?? 0);
      startPlaying();
    } else pausePlaying();
  }
  function toggleLoop(kind = null, id = null) {
    if (kind === null) loopTarget = loopTarget ? null : {kind:'section', id:currentSectionId};
    else loopTarget = loopTarget?.kind === kind && loopTarget.id === id ? null : {kind, id};
    const loop = activeLoop();
    if (loop && (selectedTime < loop.start || selectedTime >= loop.end)) seekTo(loop.start);
    refreshLoop();
  }
  function refreshLoop() {
    const loop = activeLoop();
    $('loop').setAttribute('aria-pressed', String(Boolean(loop)));
    $('loop').textContent = loop ? `↻ ${loop.label}` : '↻ Loop section';
    $('loop').title = loop ? `Stop looping ${loop.label} (${time(loop.start,true)} – ${time(loop.end,true)})` : 'Loop the current section';
    document.querySelectorAll('[data-loop-section]').forEach(n => n.setAttribute('aria-pressed', String(loopTarget?.kind === 'section' && n.dataset.loopSection === loopTarget.id)));
    document.querySelectorAll('[data-loop-phrase]').forEach(n => n.setAttribute('aria-pressed', String(loopTarget?.kind === 'phrase' && n.dataset.loopPhrase === loopTarget.id)));
  }
  function renderLyrics(row, pane) {
    const timing = node('div', 'phrase-timing');
    timing.append(makeButton(`${hasAudio ? '▶ ' : ''}${time(row.start, true)} – ${time(row.end, true)}`, 'time-button', () => seekTo(row.start, true), hasAudio ? 'Play this phrase' : 'Select this phrase'));
    if (hasAudio) {
      const loop = makeButton('↻ Loop', 'phrase-loop no-print', () => toggleLoop('phrase', row.id), 'Loop this phrase');
      loop.dataset.loopPhrase = row.id; loop.setAttribute('aria-pressed', 'false'); timing.append(loop);
    }
    if (row.cue) timing.append(makeButton('Edit', 'edit-lyric no-print', () => openLyricsEditor(row.cue.id), 'Correct this lyric line'));
    pane.append(timing);
    const lyric = node('p', 'lyric-text' + (row.cue ? '' : ' instrumental'));
    if (row.cue) {
      const words = row.cue.words || [];
      if (words.length && saved.lyrics?.[row.cue.id] === undefined) {
        words.forEach((word, i) => {
          const span = node('span', 'word', word.text);
          span.dataset.start = word.start;
          span.dataset.end = word.end;
          lyric.append(span);
          if (i < words.length - 1) lyric.append(document.createTextNode(' '));
        });
      } else lyric.textContent = lyricText(row.cue) || '(No lyric text)';
    } else lyric.textContent = ['intro','interlude','outro'].includes(row.kind) ? 'Instrumental' : 'Instrumental / lyric gap';
    pane.append(lyric);
    if (row.cue?.review) pane.append(node('span', 'lyric-footnote', 'Check this line by ear.'));
    if (!row.cue && !['intro','interlude','outro'].includes(row.kind)) pane.append(node('span', 'lyric-footnote', 'No lyric cue was detected here.'));
    if (row.events.length) {
      const summary = node('div', 'notes-summary');
      summary.append(node('span', 'notes-label', 'DETECTED NOTES'));
      const used = new Set(row.events.flatMap(e => e.positions.map(p => p.pitch % 12)));
      for (const pc of [...used].sort((a,b) => a-b)) summary.append(node('span', 'note-chip', pitches[pc]));
      pane.append(summary);
    }
  }
  function renderTab(row, pane) {
    if (!row.events.length) {
      pane.append(node('p', 'no-guitar', 'No guitar notes detected in this phrase.'));
      return;
    }
    const chunkSize = Math.ceil(row.events.length / Math.ceil(row.events.length / 20));
    for (let offset = 0; offset < row.events.length; offset += chunkSize) {
      const events = row.events.slice(offset, offset + chunkSize);
      const system = node('div', 'tab-system');
      system.append(node('div', 'system-time', `${time(events[0].start, true)} → ${time(events[events.length - 1].start, true)}`));
      const scroller = node('div', 'tab-scroll');
      const grid = node('div', 'tab-grid');
      grid.style.setProperty('--columns', events.length);
      const labels = node('div', 'tab-labels');
      labels.append(node('div', 'chord-row', 'ch.'));
      for (let s=5;s>=0;s--) labels.append(node('span', 'tab-label', base.tuning.labels[s]));
      grid.append(labels);
      events.forEach((event, i) => {
        const desc = event.positions.map(p => `string ${6-p.string}, fret ${p.fret}, ${p.note}${p.octave}`).join('; ');
        const column = makeButton('', 'tab-event', () => seekTo(event.start, true), `${time(event.start, true)}${event.chord ? ' · '+event.chord : ''} · ${desc}`);
        column.dataset.eventId = event.id;
        column.dataset.start = event.start;
        column.setAttribute('aria-label', `${time(event.start, true)}: ${desc}`);
        const previous = events[i - 1];
        const chord = node('span', 'chord-row', event.chord && previous?.chord !== event.chord ? event.chord : '');
        chord.title = event.chord ? `Detected harmony: ${event.chord}` : 'No exact chord match';
        column.append(chord);
        for (let s=5;s>=0;s--) {
          const cell = node('span', 'string-cell');
          const position = event.positions.find(p => p.string === s);
          if (position) {
            cell.append(node('span', 'fret', position.fret));
            cell.append(node('span', 'pitch-name', position.note));
          }
          column.append(cell);
        }
        grid.append(column);
        eventNodes.set(event.id, column);
      });
      scroller.append(grid);
      system.append(scroller);
      pane.append(system);
    }
  }
  function render() {
    const previousLoop = activeLoop();
    arrange();
    $('chart').replaceChildren(); $('section-nav').replaceChildren();
    rowNodes.clear(); eventNodes.clear(); cueMap.clear();
    currentRow = null; currentEvents.clear();
    const nextLoop = activeLoop();
    if (!nextLoop || (loopTarget?.kind === 'phrase' &&
        (nextLoop.start !== previousLoop?.start || nextLoop.end !== previousLoop?.end))) loopTarget = null;
    sections.forEach((section, i) => {
      const link = node('a', 'nav-link');
      link.href = `#${section.id}`;
      link.dataset.sectionId = section.id;
      link.append(node('span','nav-number',String(i+1).padStart(2,'0')), node('span','nav-name',section.label), node('span','nav-time',time(section.start)));
      link.addEventListener('click', e => { e.preventDefault(); seekTo(section.start, false); $(section.id).scrollIntoView({block:'start'}); });
      $('section-nav').append(link);
      const article = node('section', `song-section section-kind-${section.kind}`);
      article.id = section.id;
      article.setAttribute('aria-labelledby', `${section.id}-title`);
      const heading = node('div','section-heading');
      const title = node('div','section-title');
      const h2 = node('h2','',section.label); h2.id = `${section.id}-title`;
      title.append(node('span','section-index',String(i+1).padStart(2,'0')),h2,node('span','section-range',`${time(section.start)} — ${time(section.end)}`));
      const actions = node('div','section-actions no-print');
      actions.append(makeButton('▶ Play','button quiet',()=>seekTo(section.start,true)));
      const loop = makeButton('↻ Loop','button loop-button',()=>toggleLoop('section',section.id),`Loop ${section.label}`);
      loop.dataset.loopSection = section.id;
      loop.setAttribute('aria-pressed','false'); actions.append(loop);
      actions.hidden = !hasAudio;
      heading.append(title,actions); article.append(heading);
      const phrases = node('div','phrases');
      rows.filter(r=>r.sectionId===section.id).forEach(row=>{
        const phrase = node('article','phrase'); phrase.id = row.id;
        phrase.dataset.start = row.start; phrase.dataset.end = row.end;
        const lyricPane = node('div','lyric-pane'), tabPane = node('div','tab-pane');
        renderLyrics(row,lyricPane); renderTab(row,tabPane);
        phrase.append(lyricPane,tabPane); phrases.append(phrase);
        rowNodes.set(row.id,phrase);
        if(row.cue) cueMap.set(row.cue.id,row.cue);
      });
      article.append(phrases); $('chart').append(article);
    });
    const loop = activeLoop();
    if (loop && (selectedTime < loop.start || selectedTime >= loop.end)) seekTo(loop.start);
    else update(selectedTime, false);
    refreshLoop();
  }
  function update(at, allowScroll = true) {
    selectedTime = at;
    $('elapsed').textContent = time(at);
    $('seek').value = at;
    const section = activeSection(at);
    currentSectionId = section.id;
    $('now-section').textContent = section.label;
    document.querySelectorAll('.nav-link').forEach(n=>{
      const active = n.dataset.sectionId===section.id;
      n.classList.toggle('active',active);
      if (active) n.setAttribute('aria-current','location'); else n.removeAttribute('aria-current');
    });
    const row = rows.find(r=>at>=r.start && at<r.end);
    if (row?.id !== currentRow) {
      if (currentRow) rowNodes.get(currentRow)?.classList.remove('active');
      if (row) {
        const el = rowNodes.get(row.id); el.classList.add('active');
        if (allowScroll && !audio.paused && $('follow').checked && !$('editor').open) {
          const rect = el.getBoundingClientRect();
          if(rect.top<15 || rect.bottom>window.innerHeight-140) el.scrollIntoView({block:'center',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
        }
      }
      currentRow=row?.id ?? null;
    }
    const sounding = new Set();
    for (const event of base.events) {
      if (event.start > at) break;
      if (event.end > at) sounding.add(event.id);
    }
    for (const id of currentEvents) if (!sounding.has(id)) eventNodes.get(id)?.classList.remove('current');
    for (const id of sounding) {
      if (!currentEvents.has(id)) {
        const el=eventNodes.get(id); el?.classList.add('current');
        if(allowScroll && !audio.paused && $('follow').checked && el) {
          const scroller=el.closest('.tab-scroll');
          const er=el.getBoundingClientRect(), sr=scroller.getBoundingClientRect();
          if(er.left<sr.left || er.right>sr.right) scroller.scrollLeft += er.left-sr.left-scroller.clientWidth/2;
        }
      }
    }
    currentEvents = sounding;
    document.querySelectorAll('.word.current').forEach(n=>n.classList.remove('current'));
    if(currentRow) rowNodes.get(currentRow).querySelectorAll('.word').forEach(n=>n.classList.toggle('current',at>=Number(n.dataset.start)&&at<Number(n.dataset.end)));
  }
  function openLyricsEditor(id) {
    editorMode='lyrics'; editedCueId=id;
    $('editor-title').textContent='Edit this lyric line'; $('editor-fields').replaceChildren(); $('editor-error').textContent='';
    const cue=base.cues.find(c=>c.id===id);
    const hint=node('p','editor-hint',`${time(cue.start,true)} – ${time(cue.end,true)}. Your edit keeps this phrase’s timing. Word highlighting pauses for edited lines.`);
    const label=node('label','editor-field','Words');
    const textarea=node('textarea'); textarea.id='lyric-editor'; textarea.value=lyricText(cue); textarea.maxLength=2000;
    label.append(textarea); $('editor-fields').append(hint,label); $('editor').showModal(); textarea.focus();
  }
  function openSectionsEditor() {
    editorMode='sections'; $('editor-title').textContent='Edit section markers'; $('editor-fields').replaceChildren(); $('editor-error').textContent='';
    $('editor-fields').append(node('p','editor-hint','Add, remove, or rename sections. Enter starts in time order as minutes:seconds, such as 1:30.30. The next marker defines the end.'));
    const list=node('div'); list.id='section-editor-list'; $('editor-fields').append(list);
    let sequence=0;
    function appendSection(s, first=false) {
      const i=sequence++;
      const row=node('div','section-editor-row');
      row.dataset.sectionId=s.id;
      const nameLabel=node('label','editor-field','Section');
      const name=node('input'); name.value=s.label; name.maxLength=80; name.required=true; name.dataset.sectionLabel=i; nameLabel.append(name);
      const startLabel=node('label','editor-field','Starts at');
      const start=node('input'); start.value=time(s.start,true); start.required=true; start.dataset.sectionStart=i; start.disabled=first; startLabel.append(start);
      const kindLabel=node('label','editor-field','Type'), kind=node('select'); kind.dataset.sectionKind=i;
      kinds.forEach(value=>{const option=node('option','',value[0].toUpperCase()+value.slice(1));option.value=value;kind.append(option);});
      kind.value=s.kind;kindLabel.append(kind);
      const remove=makeButton('Remove','button quiet small',()=>row.remove(),`Remove ${s.label}`);remove.disabled=first;
      row.append(nameLabel,startLabel,kindLabel,remove); list.append(row);
    }
    sections.forEach((s,i)=>appendSection(s,i===0));
    const add=makeButton('Add section','button quiet',()=>{
      const starts=[...list.querySelectorAll('[data-section-start]')].map(n=>parseTime(n.value)).filter(Number.isFinite);
      const last=Math.max(0,...starts);
      appendSection({id:`section-${Date.now()}-${sequence}`,label:'Verse',kind:'verse',start:Math.min(base.duration-.01,last+(base.duration-last)/2)});
      list.lastElementChild.querySelector('input').focus();
    });
    add.id='add-section';$('editor-fields').append(add);
    $('editor').showModal();
  }
  $('edit-form').addEventListener('submit',e=>{
    e.preventDefault();
    try {
      if(editorMode==='lyrics') saved=validateCorrections({...saved, lyrics:{...saved.lyrics,[editedCueId]:$('lyric-editor').value}});
      else saved=validateCorrections({...saved,sections:[...$('section-editor-list').children].map(row=>({id:row.dataset.sectionId,label:row.querySelector('[data-section-label]').value,start:parseTime(row.querySelector('[data-section-start]').value),kind:row.querySelector('[data-section-kind]').value}))});
      persist(); $('editor').close(); render();
    } catch(error) { $('editor-error').textContent=error.message; }
  });
  for(const id of ['close-editor','cancel-editor']) $(id).addEventListener('click',()=>$('editor').close());
  $('edit-sections').addEventListener('click',openSectionsEditor);
  function downloadCorrections(corrections) {
    const blob=new Blob([JSON.stringify({schema:'songbook.corrections/v1',id:base.id,...corrections},null,2)],{type:'application/json'});
    const url=URL.createObjectURL(blob); const a=node('a'); a.href=url; a.download=`${base.title.replace(/[^\p{L}\p{N}]+/gu,'-').replace(/^-|-$/g,'') || 'Song'}-corrections.json`; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  $('export-edits').addEventListener('click',()=>downloadCorrections({revision:base.revision,...saved}));
  $('import-edits').addEventListener('change',async e=>{
    const file=e.target.files[0]; if(!file)return;
    try {
      if(file.size>maxCorrectionsBytes)throw Error('The corrections file is too large for this song.');
      const value=JSON.parse(await file.text());
      if(value.schema!=='songbook.corrections/v1'||value.id!==base.id)throw Error(`Choose a corrections file for ${base.title}.`);
      if(!value.revision)throw Error('This older file has no transcript version. Review it and reapply its edits to the matching lines.');
      saved=validateCorrections(value);persist();render();
    }catch(error){$('save-status').textContent=error.message;}
    e.target.value='';
  });
  $('reset-edits').addEventListener('click',()=>{
    if(!Object.keys(saved.lyrics||{}).length&&!saved.sections){$('save-status').textContent='No corrections to reset.';return;}
    if(confirm('Reset your lyric and section corrections? Download them first if you want to keep a copy.')){saved={};persist();render();}
  });
  $('play').addEventListener('click',togglePlay);
  $('loop').addEventListener('click',()=>toggleLoop());
  $('seek').max=base.duration;
  $('seek').addEventListener('input',e=>seekTo(Number(e.target.value),false,false));
  $('speed').addEventListener('change',()=>{audio.playbackRate=Number($('speed').value);audio.preservesPitch=true;});
  $('track').addEventListener('change',()=>{
    trackChange++;
    const source=base.audio[$('track').value];
    if (!source) return;
    pendingTrack={at:pendingTrack?.at ?? audio.currentTime, source:new URL(source,location.href).href};
    audio.src=source;
    $('now-track').textContent=`${base.title} · ${$('track').value==='guitar'?'Guitar stem':'Full song'}`;
    audio.load();
  });
  audio.addEventListener('loadedmetadata',()=>{
    if(!pendingTrack || audio.currentSrc!==pendingTrack.source) return;
    const at=Math.min(pendingTrack.at,audio.duration||base.duration);
    audio.currentTime=at; audio.playbackRate=Number($('speed').value); audio.preservesPitch=true;
    pendingTrack=null; update(at,false); if(wantPlaying)startPlaying();
  });
  function playbackUpdate() {
    if(pendingTrack)return;
    const loop=activeLoop();
    if(loop&&!audio.paused&&(audio.currentTime<loop.start||audio.currentTime>=loop.end))audio.currentTime=loop.start;
    update(audio.currentTime);
  }
  function animate() {
    frame=null;playbackUpdate();
    if(!audio.paused&&!pendingTrack)frame=requestAnimationFrame(animate);
  }
  audio.addEventListener('timeupdate',playbackUpdate);
  audio.addEventListener('play',()=>{
    if(pendingTrack || audio.paused)return;
    wantPlaying=true;refreshPlay();if(frame===null)frame=requestAnimationFrame(animate);
  });
  audio.addEventListener('pause',()=>{
    if(frame!==null){cancelAnimationFrame(frame);frame=null;}
    if(!pendingTrack){wantPlaying=false;refreshPlay();}
  });
  audio.addEventListener('ended',()=>{const loop=activeLoop();if(loop){seekTo(loop.start,true);}else update(base.duration,false);});
  audio.addEventListener('error',()=>{pendingTrack=null;wantPlaying=false;refreshPlay();$('audio-status').textContent='Audio file unavailable. Keep the exported MP3 files beside this HTML file.';});
  $('show-notes').addEventListener('change',()=>document.body.classList.toggle('no-notes',!$('show-notes').checked));
  $('show-chords').addEventListener('change',()=>document.body.classList.toggle('no-chords',!$('show-chords').checked));
  function resize(delta){tabSize=Math.max(11,Math.min(18,tabSize+delta));document.documentElement.style.setProperty('--tab-size',`${tabSize}px`);$('smaller').disabled=tabSize===11;$('larger').disabled=tabSize===18;}
  $('smaller').addEventListener('click',()=>resize(-1));$('larger').addEventListener('click',()=>resize(1));
  $('print').addEventListener('click',()=>window.print());
  document.addEventListener('keydown',e=>{
    if($('editor').open||e.target.closest('input,textarea,select,button,a,[contenteditable]')||e.ctrlKey||e.altKey||e.metaKey)return;
    if(e.code==='Space'){e.preventDefault();togglePlay();}
    if(e.code==='ArrowLeft'){e.preventDefault();seekTo(selectedTime-5);}
    if(e.code==='ArrowRight'){e.preventDefault();seekTo(selectedTime+5);}
  });
  $('total').textContent=time(base.duration);$('song-duration').textContent=time(base.duration);
  render();
  for(const option of [...$('track').options])if(!base.audio[option.value])option.remove();
  if(hasAudio) {
    $('track').dispatchEvent(new Event('change'));
  } else {
    document.querySelector('.transport').hidden=true;
    document.body.classList.add('without-audio');
    $('play-hint').textContent='This chart was exported without audio.';
    $('follow').disabled=true;
  }
  // A small read-only inspection surface for checking the generated document.
  window.songbook={get rows(){return rows;},get sections(){return sections;},get data(){return base;},time,parseTime,validateCorrections};
})();
